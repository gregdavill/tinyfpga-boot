"""End-to-end cocotb tests for the TinyFPGA serial (CDC-ACM) backend.

Build the DUT with `BACKEND=serial` (see sim/Makefile / elaborate.py) and run
the same full-stack harness as the UF2 suite: a bit-level USB host model plus a
Winbond SPI-flash model. These exercise `Top` -> CDC-ACM -> `SpiBridge` ->
QSPI controller -> flash, i.e. the exact path the `tinyprog` programmer drives.

The host frames every flash operation as one raw SPI transaction:

    0x00                              -> boot (reconfigure)
    0x01 <wlen:u16le> <rlen:u16le> <write_string> -> SPI xfer, returns rlen bytes
"""

import struct

import cocotb
from cocotb.triggers import Timer

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


DESC_DEVICE        = 0x01
DESC_CONFIGURATION = 0x02

TIMEOUT_ENUM = 2_000   # full enumeration walk
TIMEOUT_XFER = 3_000   # one framed SPI transaction round-trip


async def _bringup(dut, *, flash_uid: bytes = b"\xCA\xFE\xBA\xBE\xDE\xAD\xBE\xEF"):
    flash = SPIFlashModel(dut, uid=flash_uid)
    cocotb.start_soon(flash.run())
    host = USBHost(dut)
    await host.start()      # also waits for usb_pullup
    await host.reset_bus()
    return host, flash


async def _enumerate(host):
    await host.set_address(0x12)
    await host.set_configuration(1)


def _spi_frame(write_string, read_len):
    """Build a 0x01 SPI-transaction frame: cmd, wlen (LE16), rlen (LE16),
    then the write_string (opcode + address + data)."""
    write_string = bytes(write_string)
    return bytes([0x01]) + struct.pack("<HH", len(write_string), read_len) + write_string


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_enumeration_cdc_acm(dut):
    """The serial backend enumerates as a CDC-ACM composite advertising
    VID:PID 1d50:6130 (tinyprog's default), with a Communications interface
    (#0) and a Data interface (#1) carrying the bulk endpoints."""
    host, _ = await _bringup(dut)

    dev = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    assert dev[0] == 18
    vid = dev[8] | (dev[9] << 8)
    pid = dev[10] | (dev[11] << 8)
    assert (vid, pid) == (0x1D50, 0x6130), f"got VID/PID {vid:04x}:{pid:04x}"
    # Composite device using an Interface Association Descriptor.
    assert dev[4] == 0xEF, f"bDeviceClass = {dev[4]:#04x}, expected 0xEF (IAD)"

    await host.set_address(0x12)

    cfg_head = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=9)
    total_len = cfg_head[2] | (cfg_head[3] << 8)
    cfg = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=total_len)
    assert len(cfg) == total_len
    assert cfg[4] == 2, f"bNumInterfaces = {cfg[4]}, expected 2 (comm + data)"

    # Walk the descriptor chain: expect a CDC comm interface (class 0x02) and
    # a CDC-data interface (class 0x0a) with a bulk endpoint pair.
    saw_comm = saw_data = saw_bulk = False
    i = 0
    while i < len(cfg):
        b_length, b_type = cfg[i], cfg[i + 1]
        if b_type == 0x04:  # INTERFACE
            cls = cfg[i + 5]
            saw_comm |= (cls == 0x02)
            saw_data |= (cls == 0x0a)
        elif b_type == 0x05:  # ENDPOINT
            if cfg[i + 3] & 0x03 == 0x02:
                saw_bulk = True
        i += b_length
    assert saw_comm, "no CDC communications interface"
    assert saw_data, "no CDC data interface"
    assert saw_bulk, "no bulk endpoint"

    await host.set_configuration(1)


@cocotb.test(timeout_time=TIMEOUT_XFER, timeout_unit="us")
async def test_fast_read_transaction(dut):
    """A 0x0B fast-read frame returns the addressed flash bytes over the
    bulk IN endpoint."""
    host, flash = await _bringup(dut)
    await _enumerate(host)

    addr = 0x01_2345
    data = bytes((i * 7 + 3) & 0xFF for i in range(16))
    flash.memory[addr:addr + len(data)] = data
    flash.transactions.clear()

    # write_string = opcode 0x0B + 3 addr bytes (BE) + 1 dummy byte.
    ws = [0x0B, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, 0x00]
    await host.bulk_out(endpoint=1, payload=_spi_frame(ws, len(data)))

    # The bridge clocks each byte out of flash through the divisor-4 QSPI
    # controller, so the IN packet fills slowly — tolerate plenty of NAKs.
    got = await host.bulk_in(endpoint=1, length=len(data), max_retries=200)
    assert got == data, f"read back {got.hex()}, expected {data.hex()}"

    ops = [t.opcode for t in flash.transactions]
    assert 0x0B in ops, f"no fast-read issued (opcodes={[hex(o) for o in ops]})"
    rd = next(t for t in flash.transactions if t.opcode == 0x0B)
    assert rd.address == addr, f"read addr {rd.address:#x}, expected {addr:#x}"


@cocotb.test(timeout_time=TIMEOUT_XFER, timeout_unit="us")
async def test_write_enable_page_program(dut):
    """WREN (0x06) then page-program (0x02) frames land data in flash; a
    status read (0x05) returns the (cleared) status byte."""
    host, flash = await _bringup(dut)
    await _enumerate(host)
    flash.transactions.clear()

    addr    = 0x00_0100
    payload = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x55, 0xAA, 0x12, 0x34])

    # Write-enable.
    await host.bulk_out(endpoint=1, payload=_spi_frame([0x06], 0))
    # Page program: opcode + 3 addr bytes + payload, no read.
    ws = [0x02, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF] + list(payload)
    await host.bulk_out(endpoint=1, payload=_spi_frame(ws, 0))

    # Read status register (1 byte): WEL/WIP should be clear after the program.
    await host.bulk_out(endpoint=1, payload=_spi_frame([0x05], 1))
    status = await host.bulk_in(endpoint=1, length=1)
    assert len(status) == 1
    assert status[0] & 0x01 == 0, f"WIP still set: {status[0]:#04x}"

    await Timer(50, unit="us")

    ops = [t.opcode for t in flash.transactions]
    assert 0x06 in ops, f"no write-enable (opcodes={[hex(o) for o in ops]})"
    assert 0x02 in ops, f"no page program (opcodes={[hex(o) for o in ops]})"
    assert bytes(flash.memory[addr:addr + len(payload)]) == payload, \
        "flash memory does not match programmed payload"


@cocotb.test(timeout_time=TIMEOUT_XFER, timeout_unit="us")
async def test_boot_command_triggers_warmboot(dut):
    """A lone 0x00 byte is the boot command: it must not touch the flash
    (not an SPI transfer), and it arms SB_WARMBOOT, which fires once the bus
    has been quiet for the idle window.

    SB_WARMBOOT is a blackbox under iverilog, so we probe the internal `boot`
    wire — the same handle the UF2 done->warmboot test uses."""
    host, flash = await _bringup(dut)
    await _enumerate(host)
    flash.transactions.clear()

    await host.bulk_out(endpoint=1, payload=bytes([0x00]))

    # 0x00 is the boot command, not a SPI op — the flash must stay untouched.
    await Timer(20, unit="us")
    assert not flash.transactions, \
        f"boot byte caused SPI activity: {[hex(t.opcode) for t in flash.transactions]}"

    # After the idle window (reload_idle_cycles=1000, ~85 µs in sim) the
    # warmboot pulse must fire.
    boot_seen = False
    for _ in range(500):
        await Timer(1, unit="us")
        try:
            if int(dut.reconfigure.boot.value) == 1:
                boot_seen = True
                break
        except (AttributeError, ValueError):
            continue
    assert boot_seen, "SB_WARMBOOT.BOOT never pulsed after a 0x00 boot command"
