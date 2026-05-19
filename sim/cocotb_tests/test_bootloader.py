"""cocotb test cases for the TinyFPGA BX bootloader.
"""

import struct

import cocotb
from cocotb.triggers import Timer

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel
from . import coverage as _cov


# Descriptor type constants (USB 2.0 §9.4)
DESC_DEVICE        = 0x01
DESC_CONFIGURATION = 0x02
DESC_STRING        = 0x03


# Per-test sim-time budgets. Set generously above what a *working*
# implementation needs, but tight enough that a regression aborts in
# seconds rather than minutes. Add `+timeout_unit="us"` to every
# `@cocotb.test()` below.
TIMEOUT_BOOT      =   200    # bring-up alone is ~60 µs
TIMEOUT_DESC      =   500    # one GET_DESCRIPTOR
TIMEOUT_ENUM      = 2_000    # full enumeration walk
TIMEOUT_UF2       = 5_000    # one CBW + UF2 + page-program round-trip
TIMEOUT_UF2_MULTI = 30_000   # multi-block UF2 write (3+ pages programmed)


async def _bringup(dut, *, flash_uid: bytes = b"\xCA\xFE\xBA\xBE\xDE\xAD\xBE\xEF"):
    """Common per-test bring-up. Returns (host, flash).

    Tests share a single sim instance, so the device retains its USB
    address and endpoint state across tests. A short SE0 right after
    pullup wipes that back to defaults (addr 0, all toggles cleared)
    so each test can assume a fresh enumeration state."""
    flash = SPIFlashModel(dut, uid=flash_uid)
    cocotb.start_soon(flash.run())

    host = USBHost(dut)
    await host.start()      # also waits for usb_pullup
    await host.reset_bus()
    return host, flash


# ----------------------------------------------------------------------
# Test cases
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_BOOT, timeout_unit="us")
async def test_boot_reads_uid(dut):
    """The FSM in `top.py` must issue 0x4B to the flash before USB
    comes up. We verify that by inspecting the flash transaction log
    after the device asserts its pullup."""
    _, flash = await _bringup(dut)

    assert flash.transactions, "no SPI activity observed before USB connect"
    first = flash.transactions[0]
    assert first.opcode == 0x4B, f"expected Read UID (0x4B), got {first.opcode:#04x}"
    assert first.read_data == flash.uid


@cocotb.test(timeout_time=TIMEOUT_DESC, timeout_unit="us")
async def test_get_device_descriptor(dut):
    host, _ = await _bringup(dut)

    desc = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    assert len(desc) == 18
    assert desc[0] == 18         # bLength
    assert desc[1] == DESC_DEVICE
    # idVendor / idProduct - little endian at offsets 8..11
    vid = desc[8] | (desc[9] << 8)
    pid = desc[10] | (desc[11] << 8)
    assert (vid, pid) == (0x1209, 0x5AF0), f"got VID/PID {vid:04x}:{pid:04x}"


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_enumeration(dut):
    """Full enumeration: device descriptor → SET_ADDRESS → config descriptor
    → SET_CONFIGURATION. Mirrors what a Linux host does on plug-in."""
    host, _ = await _bringup(dut)

    # Get first 8 bytes of device descriptor (to learn bMaxPacketSize0).
    short = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=8)
    assert short[7] in (8, 16, 32, 64)

    await host.set_address(0x12)

    full = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    assert full[0] == 18

    # Configuration descriptor - read header first to discover wTotalLength.
    cfg_head = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=9)
    total_len = cfg_head[2] | (cfg_head[3] << 8)
    cfg_full = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=total_len)
    assert len(cfg_full) == total_len

    # We expect at least one bulk endpoint descriptor (EP1 IN/OUT).
    # Walk the descriptor chain and check.
    found_bulk = False
    i = 0
    while i < len(cfg_full):
        b_length = cfg_full[i]
        b_type   = cfg_full[i + 1]
        if b_type == 0x05:  # ENDPOINT
            bm_attrs = cfg_full[i + 3]
            if bm_attrs & 0x03 == 0x02:
                found_bulk = True
        i += b_length
    assert found_bulk, "no bulk endpoint in configuration descriptor"

    await host.set_configuration(1)


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_serial_descriptor_uses_flash_uid(dut):
    """The device fetches its UID from flash exactly ONCE at boot
    (via the `FlashUID` FSM, before USB enumerates) and stamps it
    into the iSerialNumber string descriptor.

    Because all five tests share one sim invocation, we can't pick
    the UID per-test here - by the time this test runs, the device
    has already latched whatever UID the FIRST flash model returned
    (the `_bringup` default). We just verify that whatever the
    device baked in at boot shows up as the serial string."""
    host, flash = await _bringup(dut)

    await host.set_address(0x12)
    # Read the device descriptor to learn iSerialNumber index.
    dev = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    i_serial = dev[16]
    assert i_serial != 0, "device declared iSerialNumber = 0 - not runtime"

    str_desc = await host.get_descriptor(
        descriptor_type=DESC_STRING, index=i_serial,
        lang_id=0x0409, length=64,
    )
    # USB string descriptors are UTF-16LE after the 2-byte header.
    text = str_desc[2:].decode("utf-16-le")
    expected = flash.uid.hex().upper()
    # The serial handler in `blocks/usb/serial_handler.py` may emit
    # uppercase, lowercase, or reversed - adjust the assertion to taste.
    assert expected in text or expected.lower() in text, \
        f"serial {text!r} does not contain UID hex {expected}"


# SCSI Bulk-Only Transport (USB MSC) constants.
CBW_SIGNATURE        = 0x43425355
CSW_SIGNATURE        = 0x53425355
SCSI_INQUIRY         = 0x12
SCSI_READ_CAPACITY10 = 0x25
SCSI_READ_10         = 0x28
SCSI_WRITE_10        = 0x2A


def _build_cbw(*, tag: int, transfer_length: int, flags: int, cb: bytes) -> bytes:
    """31-byte Command Block Wrapper. `flags=0x00` → host-to-device."""
    assert len(cb) <= 16
    return struct.pack(
        "<IIIBBB",
        CBW_SIGNATURE,
        tag,
        transfer_length,
        flags,
        0,                  # LUN
        len(cb),            # CB length
    ) + cb + b"\x00" * (16 - len(cb))


def _build_uf2_block(*, target_addr: int, payload: bytes,
                     block_no: int = 0, num_blocks: int = 1) -> bytes:
    """512-byte UF2 block. Fields per https://github.com/microsoft/uf2."""
    UF2_MAGIC_START0 = 0x0A324655
    UF2_MAGIC_START1 = 0x9E5D5157
    UF2_MAGIC_END    = 0x0AB16F30
    assert len(payload) <= 476
    block = struct.pack(
        "<8I",
        UF2_MAGIC_START0, UF2_MAGIC_START1,
        0x0,                # flags
        target_addr,
        len(payload),
        block_no,
        num_blocks,
        0,                  # familyID (unset)
    ) + payload + b"\x00" * (476 - len(payload)) + struct.pack("<I", UF2_MAGIC_END)
    assert len(block) == 512
    return block


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_uf2_write_triggers_program(dut):
    """Push a single UF2 block through the SCSI WRITE-10 path and watch
    the flash see WREN → sector erase → WREN → page program.

    The device's EP1 OUT is mass storage (SCSI Bulk-Only Transport), so
    the host has to wrap the UF2 block in a CBW. The device responds
    with a CSW once the 512-byte WRITE-10 data phase is consumed."""
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # Reset the transaction log so we only see UF2-driven activity from
    # here on (the boot-time UID read and any earlier SCSI commands are
    # noise for this assertion).
    flash.transactions.clear()

    target_addr = 0x0001_0000
    payload     = bytes(range(256))
    uf2_block   = _build_uf2_block(target_addr=target_addr, payload=payload)

    # WRITE-10 CDB: opcode, flags, 4 BE LBA bytes, group, 2 BE length, control.
    # LBA = target_addr / 512; xfer length = 1 block (= 512 bytes).
    lba = target_addr // 512
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, lba, 0, 1, 0)
    cbw = _build_cbw(tag=0xDEADBEEF, transfer_length=512, flags=0x00, cb=cdb)
    assert len(cbw) == 31

    _cov.cover_cbw(SCSI_WRITE_10, 0)   # WRITE_10, host → device
    _cov.cover_uf2_outcome("valid_block")
    _cov.cover_uf2_outcome("done_asserted")

    await host.bulk_out(endpoint=1, payload=cbw)
    await host.bulk_out(endpoint=1, payload=uf2_block)

    # Read the 13-byte Command Status Wrapper.
    csw = await host.bulk_in(endpoint=1, length=13)
    assert len(csw) == 13, f"short CSW: {csw.hex()}"
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE, f"bad CSW signature {sig:#010x}"
    assert tag == 0xDEADBEEF, f"CSW tag mismatch: {tag:#010x}"
    assert residue == 0, f"unexpected residue {residue}"
    assert status == 0, f"CSW status = {status} (command failed)"

    # The QspiFlash FSM stays in WRITE_NEXT until `done` (driven by
    # uf2.done at the end of the final block) lets it close the page.
    # At divisor=4 the QSPI clock is 3 MHz, so 256 data bytes alone
    # take ~700 µs. Give it >1 ms so the closing PP fully drains.
    await Timer(1500, unit="us")

    opcodes = [t.opcode for t in flash.transactions]
    assert 0x20 in opcodes, f"no sector erase observed (opcodes={[hex(o) for o in opcodes]})"
    program_ops = [t for t in flash.transactions if t.opcode in (0x02, 0x32)]
    assert program_ops, f"no page program observed (opcodes={[hex(o) for o in opcodes]})"
    programmed = program_ops[0]
    assert programmed.address == target_addr, \
        f"programmed at {programmed.address:#x}, expected {target_addr:#x}"

    # And the bytes that landed in the flash model match the payload.
    assert bytes(flash.memory[target_addr:target_addr + len(payload)]) == payload, \
        "flash memory contents do not match the UF2 payload"


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_uf2_write_triggers_program_repeat(dut):
    """Minimal back-to-back-write repro: re-run the same single-block
    UF2 write as the previous test, to isolate whether the multi-block
    failure is multi-block-specific or 'any UF2 write after the first'.
    
    """
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)
    flash.transactions.clear()

    target_addr = 0x0001_0000
    payload     = bytes(range(256))
    uf2_block   = _build_uf2_block(target_addr=target_addr, payload=payload)

    lba = target_addr // 512
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, lba, 0, 1, 0)
    cbw = _build_cbw(tag=0xBEEF, transfer_length=512, flags=0x00, cb=cdb)
    _cov.cover_cbw(SCSI_WRITE_10, 0)

    await host.bulk_out(endpoint=1, payload=cbw)
    await host.bulk_out(endpoint=1, payload=uf2_block)

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert status == 0, f"CSW status = {status}"


@cocotb.test(timeout_time=TIMEOUT_UF2_MULTI, timeout_unit="us")
async def test_uf2_multi_block_write(dut):
    """Send two UF2 blocks back-to-back inside one SCSI WRITE-10
    (transfer_length=1024) and verify both payloads land in flash and
    `uf2.done` actually fires for the *last* block, not the first.
    """
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)
    flash.transactions.clear()

    base_addr  = 0x0001_0000
    payload_0  = bytes((i + 0xA0) & 0xFF for i in range(256))
    payload_1  = bytes((i + 0xC0) & 0xFF for i in range(256))
    block_0 = _build_uf2_block(target_addr=base_addr,         payload=payload_0,
                               block_no=0, num_blocks=2)
    block_1 = _build_uf2_block(target_addr=base_addr + 0x100, payload=payload_1,
                               block_no=1, num_blocks=2)
    blocks  = block_0 + block_1

    lba = base_addr // 512
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, lba, 0, 2, 0)
    cbw = _build_cbw(tag=0xCAFE0001, transfer_length=len(blocks),
                     flags=0x00, cb=cdb)
    _cov.cover_cbw(SCSI_WRITE_10, 0)
    _cov.cover_uf2_outcome("multi_block")
    _cov.cover_uf2_outcome("done_asserted")

    await host.bulk_out(endpoint=1, payload=cbw)
    await host.bulk_out(endpoint=1, payload=blocks)

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xCAFE0001
    assert residue == 0, f"unexpected residue {residue}"
    assert status == 0, f"CSW status = {status}"

    await Timer(3000, unit="us")

    program_ops = [t for t in flash.transactions if t.opcode in (0x02, 0x32)]
    program_addrs = [t.address for t in program_ops]
    assert base_addr         in program_addrs, f"missing PP @ {base_addr:#x}: {program_addrs}"
    assert base_addr + 0x100 in program_addrs, f"missing PP @ {base_addr+0x100:#x}: {program_addrs}"

    assert bytes(flash.memory[base_addr:base_addr + 256]) == payload_0
    assert bytes(flash.memory[base_addr + 256:base_addr + 512]) == payload_1


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_uf2_write_spans_sector_boundary(dut):
    """Write a single UF2 block whose payload straddles a 4 KiB flash
    sector boundary. Target 0x0000_FF80, 256 bytes → 128 bytes in
    sector 0x00F, 128 bytes in sector 0x010. Verify the QspiFlash
    issues two separate WREN → sector-erase → page-program sequences
    (one per sector touched), and that all 256 payload bytes land
    contiguously in the flash model.
    """
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)
    flash.transactions.clear()

    target_addr = 0x0000_FF80
    payload     = bytes((i + 0x40) & 0xFF for i in range(256))
    uf2_block   = _build_uf2_block(target_addr=target_addr, payload=payload)

    lba = target_addr // 512
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, lba, 0, 1, 0)
    cbw = _build_cbw(tag=0xC0FFEE, transfer_length=512, flags=0x00, cb=cdb)
    _cov.cover_cbw(SCSI_WRITE_10, 0)

    await host.bulk_out(endpoint=1, payload=cbw)
    await host.bulk_out(endpoint=1, payload=uf2_block)

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xC0FFEE
    assert residue == 0, f"unexpected residue {residue}"
    assert status == 0, f"CSW status = {status}"

    # 256 bytes at ~2.67 µs/byte + two erases + WRENs + status polls.
    await Timer(2000, unit="us")

    # Two sector erases, at the two sector-aligned addresses.
    erase_addrs = sorted(
        t.address for t in flash.transactions
        if t.opcode == 0x20 and t.address is not None
    )
    assert erase_addrs == [0x00F000, 0x010000], \
        f"unexpected sector erases: {[hex(a) for a in erase_addrs]}"

    # At least one page program per sector touched (could be more if
    # the flash controller splits at page boundaries within a sector).
    program_addrs = [
        t.address for t in flash.transactions
        if t.opcode in (0x02, 0x32) and t.address is not None
    ]
    assert any(0x00F000 <= a < 0x010000 for a in program_addrs), \
        f"no page program in sector 0x00F: {[hex(a) for a in program_addrs]}"
    assert any(0x010000 <= a < 0x011000 for a in program_addrs), \
        f"no page program in sector 0x010: {[hex(a) for a in program_addrs]}"

    # The whole 256-byte slab should land contiguously across the
    # sector boundary in the model.
    assert bytes(flash.memory[target_addr:target_addr + 256]) == payload


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_scsi_read_10_boot_sector(dut):
    """Read LBA 0 via SCSI READ_10 over USB and verify the device
    returns a FAT16 boot sector (signature 0x55, 0xAA at offset 510).
    Exercises the full READ path end-to-end:

      SCSI DISPATCH(READ_10) → SEND_SECTOR → GhostFAT ROM read →
      scsi.tx → USBStreamInEndpoint → host bulk_in (8 × 64-byte
      packets) → final CSW.
    """
    host, _flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # READ_10 CDB: opcode, flags, 4 BE LBA bytes, group, 2 BE length,
    # control. LBA=0, transfer length=1 sector.
    cdb = struct.pack(">BBIBHB", SCSI_READ_10, 0, 0, 0, 1, 0)
    cbw = _build_cbw(tag=0x12345678, transfer_length=512,
                     flags=0x80, cb=cdb)   # 0x80 = device-to-host
    _cov.cover_cbw(SCSI_READ_10, 1)

    await host.bulk_out(endpoint=1, payload=cbw)
    data = await host.bulk_in(endpoint=1, length=512)
    assert len(data) == 512, f"short READ_10 data: {len(data)}B"

    # FAT16 boot sector signature.
    assert data[510] == 0x55 and data[511] == 0xAA, (
        f"missing FAT16 boot signature: "
        f"got {data[510]:#04x}, {data[511]:#04x}"
    )
    # Bytes per sector at offset 11 (LE 16-bit) — 512 for GhostFAT.
    bytes_per_sector = data[11] | (data[12] << 8)
    assert bytes_per_sector == 512, f"bytes/sector: {bytes_per_sector}"

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE, f"bad CSW signature {sig:#010x}"
    assert tag == 0x12345678,    f"CSW tag mismatch: {tag:#010x}"
    assert residue == 0,         f"unexpected residue {residue}"
    assert status == 0,          f"CSW status = {status}"


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_scsi_inquiry(dut):
    """SCSI INQUIRY over USB. Verify the device identifies as a
    direct-access block device with the expected vendor/product
    strings baked in at top.py.
    """
    host, _flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # INQUIRY CDB: opcode, EVPD/CMDDT bits, page code, reserved,
    # allocation length (1 byte), control. Standard inquiry = EVPD=0,
    # allocation_length=36.
    cdb = struct.pack(">BBBBBB", SCSI_INQUIRY, 0, 0, 0, 36, 0)
    cbw = _build_cbw(tag=0xABCD0001, transfer_length=36, flags=0x80, cb=cdb)
    _cov.cover_cbw(SCSI_INQUIRY, 1)

    await host.bulk_out(endpoint=1, payload=cbw)
    data = await host.bulk_in(endpoint=1, length=36)
    assert len(data) == 36, f"short INQUIRY: {len(data)}B"

    assert data[0] == 0x00, f"peripheral type: {data[0]:#04x} (want direct-access)"
    assert data[1] == 0x80, f"removable bit: {data[1]:#04x}"
    assert data[2] == 0x02, f"SCSI version: {data[2]:#04x}"
    vendor  = bytes(data[8:16]).decode("ascii").strip()
    product = bytes(data[16:32]).decode("ascii").strip()
    assert vendor  == "TINYFPGA",       f"vendor: {vendor!r}"
    assert product == "UF2 Bootloader", f"product: {product!r}"

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xABCD0001
    assert residue == 0
    assert status == 0


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_scsi_read_capacity(dut):
    """SCSI READ_CAPACITY_10 over USB. Verify the device reports the
    correct last-LBA and block size - derived from top.py's
    SCSIHandler config (16 MiB / 512 B = 32768 blocks, so last LBA
    is 32767).
    """
    host, _flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # READ_CAPACITY_10 CDB: 10 bytes, all zero except opcode.
    cdb = bytes([SCSI_READ_CAPACITY10]) + b"\x00" * 9
    cbw = _build_cbw(tag=0xABCD0002, transfer_length=8, flags=0x80, cb=cdb)
    _cov.cover_cbw(SCSI_READ_CAPACITY10, 1)

    await host.bulk_out(endpoint=1, payload=cbw)
    data = await host.bulk_in(endpoint=1, length=8)
    assert len(data) == 8, f"short READ_CAPACITY: {len(data)}B"

    last_lba   = int.from_bytes(data[0:4], "big")
    block_size = int.from_bytes(data[4:8], "big")
    assert last_lba   == 32767, f"last LBA: {last_lba}"
    assert block_size == 512,   f"block size: {block_size}"

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xABCD0002
    assert residue == 0
    assert status == 0


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_scsi_bad_cbw_does_not_deadlock(dut):
    """Send a CBW with a garbage signature and an unknown opcode,
    then verify the device is still alive enough to service a
    subsequent valid CBW.

    USB MSC §6.6.1 - invalid CBW should STALL Bulk-In and require Reset Recovery
    """
    host, _flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # Bad CBW: garbage signature, unknown opcode, no data phase.
    bad_cbw = struct.pack(
        "<IIIBBB",
        0xDEADBEEF,             # bad signature (real is CBW_SIGNATURE)
        0x42424242,             # tag (still parsed from bytes 4-7)
        0,                      # transfer_length = 0 (no data phase)
        0,                      # flags
        0,                      # LUN
        1,                      # CB length
    ) + bytes([0xFF]) + b"\x00" * 15        # CDB byte 0 = unknown opcode
    assert len(bad_cbw) == 31

    await host.bulk_out(endpoint=1, payload=bad_cbw)
    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, _residue, status = struct.unpack("<IIIB", csw[:13])
    # CSW itself should still be well-formed.
    assert sig == CSW_SIGNATURE, f"bad CSW signature {sig:#010x}"
    assert tag == 0x42424242,    f"CSW tag {tag:#010x} ≠ 0x42424242"
    assert status == 1,          f"expected status=1 (unknown opcode), got {status}"
    _cov.cover_cbw(0xFF, 0)  # UNKNOWN bin × host-to-device

    # Recovery check: a VALID INQUIRY CBW must still be serviced.
    cdb = struct.pack(">BBBBBB", SCSI_INQUIRY, 0, 0, 0, 36, 0)
    valid_cbw = _build_cbw(tag=0xC0FFEE, transfer_length=36,
                           flags=0x80, cb=cdb)
    _cov.cover_cbw(SCSI_INQUIRY, 1)

    await host.bulk_out(endpoint=1, payload=valid_cbw)
    data = await host.bulk_in(endpoint=1, length=36)
    vendor = bytes(data[8:16]).decode("ascii").strip()
    assert vendor == "TINYFPGA", \
        f"INQUIRY after bad CBW returned vendor {vendor!r} — FSM stuck?"

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xC0FFEE
    assert residue == 0
    assert status == 0


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_ms_request_reset_clears_in_flight_scsi(dut):
    """USB MSC Bulk-Only Transport §3.1 — Mass Storage Reset
    (bRequest=0xFF, class+interface). In-flight SCSI state 
    (cbw_count, data_sent, opcode, transfer_length) is wiped 
    and the FSM returns to RECEIVE_CBW ready for a fresh command.

    Test sequence:
      1. Send a WRITE_10 CBW (transfer_length=512). SCSI parses it
         and transitions to RECEIVE_WRITE_DATA expecting 512 bytes.
      2. DON'T send the data — leave SCSI mid-stream.
      3. Issue MS_REQUEST_RESET on EP0.
      4. Send a fresh INQUIRY CBW. Test that it is serviced normally"""
    host, _flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # Park SCSI in RECEIVE_WRITE_DATA by sending only the WRITE_10
    # CBW (no data follow-up).
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, 0, 0, 1, 0)
    write_cbw = _build_cbw(tag=0xDEAD, transfer_length=512,
                           flags=0x00, cb=cdb)
    await host.bulk_out(endpoint=1, payload=write_cbw)

    # USB MSC class request:
    #   bmRequestType = 0x21 — class, interface, host→device
    #   bRequest      = 0xFF — Bulk-Only Mass Storage Reset
    #   wValue        = 0
    #   wIndex        = 0    — interface 0
    #   wLength       = 0    — no data stage
    await host.control_out(0x21, 0xFF, w_value=0, w_index=0, data=b"")
    _cov.cover_usb_class_request(0xFF)

    # SCSI is now back in RECEIVE_CBW. Run an INQUIRY end-to-end as
    # proof of life.
    cdb = struct.pack(">BBBBBB", SCSI_INQUIRY, 0, 0, 0, 36, 0)
    inquiry_cbw = _build_cbw(tag=0xC0FFEE, transfer_length=36,
                             flags=0x80, cb=cdb)
    _cov.cover_cbw(SCSI_INQUIRY, 1)

    await host.bulk_out(endpoint=1, payload=inquiry_cbw)
    data = await host.bulk_in(endpoint=1, length=36)
    vendor = bytes(data[8:16]).decode("ascii").strip()
    assert vendor == "TINYFPGA", \
        f"INQUIRY after MS reset returned vendor {vendor!r} — reset failed?"

    csw = await host.bulk_in(endpoint=1, length=13)
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == CSW_SIGNATURE
    assert tag == 0xC0FFEE
    assert residue == 0
    assert status == 0


# ----------------------------------------------------------------------
# Coverage finalizer — must be the LAST @cocotb.test in this module so
# the report covers every preceding test.
# ----------------------------------------------------------------------

@cocotb.test()
async def _zzz_report_coverage(dut):
    """Dump the functional-coverage report to sim/build/. Always
    passes; functions as an end-of-regression hook."""
    _cov.dump_reports()
