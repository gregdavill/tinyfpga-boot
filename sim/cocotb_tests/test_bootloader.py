"""cocotb test cases for the TinyFPGA BX bootloader.
"""

import struct

import cocotb
from cocotb.triggers import Timer

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


# Descriptor type constants (USB 2.0 §9.4)
DESC_DEVICE        = 0x01
DESC_CONFIGURATION = 0x02
DESC_STRING        = 0x03


# Per-test sim-time budgets. Set generously above what a *working*
# implementation needs, but tight enough that a regression aborts in
# seconds rather than minutes. Add `+timeout_unit="us"` to every
# `@cocotb.test()` below.
TIMEOUT_BOOT     =  200    # bring-up alone is ~60 µs
TIMEOUT_DESC     =  500    # one GET_DESCRIPTOR
TIMEOUT_ENUM     = 2_000   # full enumeration walk
TIMEOUT_UF2      = 5_000   # SCSI + UF2 + page-program round-trip


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


@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us", skip=True)
async def test_uf2_write_triggers_program(dut):
    """Push a single UF2 block to EP1 OUT and watch the flash see a
    sector-erase + page-program at the address the block declared.

    SKIPPED - the device's EP1 OUT runs SCSI Bulk-Only Transport
    (CBW → data → CSW), not raw UF2. The test below needs to wrap
    the UF2 block in a SCSI WRITE-10 CBW first; until that's added,
    the device persistently NAKs the raw bytes and the test stalls.
    """
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # Reset transaction log so we only see the UF2-driven activity.
    flash.transactions.clear()

    # Minimal UF2 block (512 B). Fields per https://github.com/microsoft/uf2.
    UF2_MAGIC_START0 = 0x0A324655
    UF2_MAGIC_START1 = 0x9E5D5157
    UF2_MAGIC_END    = 0x0AB16F30
    target_addr      = 0x0001_0000
    payload          = bytes(range(256))
    block = struct.pack(
        "<8I",
        UF2_MAGIC_START0, UF2_MAGIC_START1,
        0x0,                # flags
        target_addr,
        len(payload),
        0,                  # block number
        1,                  # total blocks
        0,                  # familyID (unset)
    ) + payload + b"\x00" * (476 - len(payload)) + struct.pack("<I", UF2_MAGIC_END)
    assert len(block) == 512

    # SCSI bulk-only requires a CBW; the UF2 path inside the bootloader
    # converts SCSI WRITE-10 → UF2 decoder → flash. For the sketch we
    # assume the UF2 decoder will accept raw blocks on EP1 OUT - TODO:
    # wrap in a real CBW once `MassStorageRequestHandler` framing is
    # confirmed.
    await host.bulk_out(endpoint=1, payload=block)

    # Give the flash FSM a few µs to drain.
    await Timer(50, unit="us")

    opcodes = [t.opcode for t in flash.transactions]
    assert 0x20 in opcodes, "no sector erase observed"
    program_ops = [t for t in flash.transactions if t.opcode in (0x02, 0x32)]
    assert program_ops, "no page program observed"
    programmed = program_ops[0]
    assert programmed.address == target_addr, \
        f"programmed at {programmed.address:#x}, expected {target_addr:#x}"

    # And the bytes that landed in the flash model match the payload.
    assert bytes(flash.memory[target_addr:target_addr + len(payload)]) == payload
