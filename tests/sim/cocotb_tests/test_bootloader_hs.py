"""cocotb test cases for the (ECP5 / ULPI) high-speed bootloader.

enumeration via a ULPI PHY + chirp handshake
HS-only device-qualifier / other-speed descriptors
512-byte bulk packets through the SCSI/UF2 write path
SOF handling. 

The DUT and host wire model differ from the FS suite, but the SCSI /
UF2 / descriptor logic under test is shared, so we reuse the FS helpers.
"""

import struct

import cocotb
from cocotb.triggers import Timer

from .usb_host_hs import USBHostHS
from .spi_flash_model import SPIFlashModel
from .dut_pins import attach_hs
from .usb_packets import build_sof
from . import coverage as _cov
from .test_bootloader import (
    DESC_DEVICE, DESC_CONFIGURATION,
    SCSI_WRITE_10, _build_cbw, _build_uf2_block,
)


DESC_DEVICE_QUALIFIER = 0x06
DESC_OTHER_SPEED      = 0x07

HS_BULK_MPS = 512

RELOAD_IMAGE_OFFSET = 0x200000

def _phys(logical_addr: int) -> int:
    """Logical UF2 address -> physical flash address."""
    return logical_addr + RELOAD_IMAGE_OFFSET

# Per-test sim-time budgets (µs). HS bring-up includes the ULPI Tstart + the
# chirp handshake (shortened in elaborate.py), plus the boot UID read.
TIMEOUT_ENUM      =  3_000
TIMEOUT_DESC      =  3_000
TIMEOUT_UF2       =  8_000
TIMEOUT_SOF       =  5_000


async def _bringup(dut, *, flash_uid: bytes = b"\xCA\xFE\xBA\xBE\xDE\xAD\xBE\xEF",
                   wip_polls_after_write: int = 0):
    """Bring up the HS DUT: flash model + ULPI host, reset, chirp into HS."""
    flash = SPIFlashModel(dut, uid=flash_uid, pins=attach_hs(dut),
                          wip_polls_after_write=wip_polls_after_write)
    cocotb.start_soon(flash.run())

    host = USBHostHS(dut)
    await host.start()        # waits for the boot read + connect
    await host.reset_bus()    # SE0 reset + high-speed chirp handshake
    return host, flash


def _find_bulk_mps(cfg: bytes):
    """Return the wMaxPacketSize of the first bulk endpoint in a config blob."""
    i = 0
    while i < len(cfg):
        b_length = cfg[i]
        b_type   = cfg[i + 1]
        if b_type == 0x05 and (cfg[i + 3] & 0x03) == 0x02:   # ENDPOINT, bulk
            return cfg[i + 4] | (cfg[i + 5] << 8)
        i += b_length
    return None


# ----------------------------------------------------------------------
# High-speed enumeration (incl. chirp) with 512-byte bulk endpoints
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_hs_enumeration(dut):
    """Full high-speed enumeration over ULPI: chirp handshake, device +
    configuration descriptors, SET_ADDRESS / SET_CONFIGURATION, and the bulk
    endpoints advertising the 512-byte high-speed max packet size."""
    host, _ = await _bringup(dut)

    short = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=8)
    assert short[7] == 64, f"bMaxPacketSize0 should be 64, got {short[7]}"

    await host.set_address(0x12)

    full = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    assert full[0] == 18 and full[1] == DESC_DEVICE

    cfg_head = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=9)
    total_len = cfg_head[2] | (cfg_head[3] << 8)
    cfg_full = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION,
                                         length=total_len)
    assert len(cfg_full) == total_len

    mps = _find_bulk_mps(cfg_full)
    assert mps == HS_BULK_MPS, f"HS bulk endpoint should be 512, got {mps}"

    await host.set_configuration(1)


# ----------------------------------------------------------------------
# HS device-qualifier + other-speed-configuration descriptors
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_DESC, timeout_unit="us")
async def test_hs_device_qualifier_and_other_speed(dut):
    """A high-speed-capable device must answer GET_DESCRIPTOR for the device
    qualifier (type 6) and other-speed configuration (type 7) rather than
    stalling. The other-speed view describes full-speed (64-byte) endpoints."""
    host, _ = await _bringup(dut)
    await host.set_address(0x12)

    dq = await host.get_descriptor(descriptor_type=DESC_DEVICE_QUALIFIER, length=10)
    assert len(dq) == 10, f"device qualifier should be 10 bytes, got {dq.hex()}"
    assert dq[1] == DESC_DEVICE_QUALIFIER
    assert dq[7] == 64, "device qualifier bMaxPacketSize0 should be 64"

    osc_head = await host.get_descriptor(descriptor_type=DESC_OTHER_SPEED, length=9)
    assert osc_head[1] == DESC_OTHER_SPEED, "type byte should be OTHER_SPEED (7)"
    total_len = osc_head[2] | (osc_head[3] << 8)
    osc_full = await host.get_descriptor(descriptor_type=DESC_OTHER_SPEED,
                                         length=total_len)
    assert _find_bulk_mps(osc_full) == 64, "other-speed bulk endpoint should be 64"


# ----------------------------------------------------------------------
# 512-byte bulk multi-packet through the SCSI / UF2 write path
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_UF2, timeout_unit="us")
async def test_hs_uf2_write_512_byte_packets(dut):
    """Drive a UF2 write at high speed. The 31-byte CBW fits one packet, but
    the 512-byte UF2 block is exactly one HS bulk packet; the WRITE-10 path
    must consume it and program the flash, then return a good CSW."""
    host, flash = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)
    flash.transactions.clear()

    target_addr = 0x0001_0000
    payload     = bytes(range(256))
    uf2_block   = _build_uf2_block(target_addr=target_addr, payload=payload)

    lba = target_addr // 512
    cdb = struct.pack(">BBIBHB", SCSI_WRITE_10, 0, lba, 0, 1, 0)
    cbw = _build_cbw(tag=0xDEADBEEF, transfer_length=512, flags=0x00, cb=cdb)

    _cov.cover_cbw(SCSI_WRITE_10, 0)
    _cov.cover_uf2_outcome("valid_block")
    _cov.cover_uf2_outcome("done_asserted")

    await host.bulk_out(endpoint=1, payload=cbw, max_packet=HS_BULK_MPS)
    await host.bulk_out(endpoint=1, payload=uf2_block, max_packet=HS_BULK_MPS)

    csw = await host.bulk_in(endpoint=1, length=13, max_packet=HS_BULK_MPS)
    assert len(csw) == 13, f"short CSW: {csw.hex()}"
    sig, tag, residue, status = struct.unpack("<IIIB", csw[:13])
    assert sig == 0x53425355, f"bad CSW signature {sig:08x}"
    assert tag == 0xDEADBEEF
    assert status == 0, f"CSW status not good: {status}"

    # The relocated payload must have landed in flash.
    phys = _phys(target_addr)
    assert flash.memory[phys:phys + len(payload)] == payload, \
        "UF2 payload did not reach flash at the relocated address"

    # And the program must have used a real page-program opcode.
    opcodes = [t.opcode for t in flash.transactions]
    assert 0x02 in opcodes or 0x32 in opcodes, \
        f"no page-program in flash ops: {[hex(o) for o in opcodes]}"


# ----------------------------------------------------------------------
# Microframe / SOF handling
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_SOF, timeout_unit="us")
async def test_hs_sof_keepalive(dut):
    """At high speed the host issues a SOF every 125 µs. Feed the device a run
    of SOF tokens with HS microframe spacing and confirm the token path
    handles them and the device still services a control transfer afterwards."""
    host, _ = await _bringup(dut)
    await host.set_address(0x12)
    await host.set_configuration(1)

    # 8 microframes (= one 1 ms frame). Spacing is shortened from the real
    # 125 µs so the test stays inside its budget while still exercising the
    # SOF token path repeatedly.
    for frame in range(8):
        await host.send_packet(build_sof(frame))
        await Timer(5, unit="us")

    # The device must still be responsive after the SOF burst.
    full = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    assert full[0] == 18 and full[1] == DESC_DEVICE
