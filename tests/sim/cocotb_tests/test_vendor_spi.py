"""cocotb test for the EP0 SPI bridge

Drives flash transactions over USB *control* transfers. Check they
reach QSPI flash

    EXEC  (0x01, OUT): wValue = read length, data = bytes to clock out.
    RESULT(0x02, IN) : returns the bytes captured by the last EXEC.
"""

import cocotb
from cocotb.triggers import Timer

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


DESC_CONFIGURATION = 0x02

RT_VENDOR_OUT = 0x40   # host->device | vendor | device
RT_VENDOR_IN  = 0xC0   # device->host | vendor | device
REQ_EXEC   = 0x01
REQ_RESULT = 0x02

SEC_READ = 0x48        # Read Security Registers (handled by SPIFlashModel)
RDID     = 0x9F        # JEDEC ID
WREN     = 0x06

TIMEOUT = 4_000


async def _bringup(dut):
    flash = SPIFlashModel(dut)
    cocotb.start_soon(flash.run())
    host = USBHost(dut)
    await host.start()
    await host.reset_bus()
    return host, flash


async def _enumerate(host):
    await host.get_descriptor(descriptor_type=0x01, length=18)   # latch MPS0
    await host.set_address(0x12)
    head = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=9)
    total = head[2] | (head[3] << 8)
    await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=total)
    await host.set_configuration(1)


# A flash read at divisor=4 is ~3.5 us/byte, a full-register read can hold
# the control transfer for hundreds of us
_RETRIES = 4000


async def _spi(host, write_bytes, read_len=0):
    """One EXEC (+ RESULT when reading); returns the read bytes."""
    await host.control_out(RT_VENDOR_OUT, REQ_EXEC, w_value=read_len,
                           data=bytes(write_bytes), max_retries=_RETRIES)
    if read_len:
        return await host.control_in(RT_VENDOR_IN, REQ_RESULT,
                                     w_length=read_len, max_retries=_RETRIES)
    return b""


@cocotb.test(timeout_time=TIMEOUT, timeout_unit="us")
async def test_vendor_read_security_register(dut):
    """EXEC clocks out 0x48 + address + dummy, RESULT returns the security
    register bytes."""
    host, flash = await _bringup(dut)
    await _enumerate(host)

    n = len(flash.security_data)
    assert n > 64, "expected the security blob to span >1 EP0 packet"
    got = await _spi(host, [SEC_READ, 0x00, 0x10, 0x00, 0x00], read_len=n)
    assert got == flash.security_data, \
        f"vendor read {got!r} != flash security_data {flash.security_data!r}"


@cocotb.test(timeout_time=TIMEOUT, timeout_unit="us")
async def test_vendor_read_jedec_id(dut):
    """EXEC clocks out 0x9F (no address/dummy) and RESULT returns the JEDEC ID."""
    host, flash = await _bringup(dut)
    await _enumerate(host)

    got = await _spi(host, [RDID], read_len=3)
    assert got == flash.jedec_id, \
        f"vendor JEDEC read {got.hex()} != model {flash.jedec_id.hex()}"


@cocotb.test(timeout_time=TIMEOUT, timeout_unit="us")
async def test_vendor_program_security_register(dut):
    """The full provisioning flow: WREN -> erase -> WREN -> program a >64-byte
    JSON blob -> read back."""
    import json
    host, flash = await _bringup(dut)
    await _enumerate(host)

    blob = json.dumps({"boardmeta": {"name": "OrangeCrab r0.2",
                                     "uuid": "019E8D37-E95F-79EC-A060-779B6C8BB309"}}).encode()
    addr = [0x00, 0x10, 0x00]                       # security register 1 @ 0x1000

    await _spi(host, [WREN])
    await _spi(host, [0x44] + addr)                 # erase security register
    await _spi(host, [WREN])
    await _spi(host, [0x42] + addr + list(blob))    # program (multi-packet OUT)

    got = await _spi(host, [SEC_READ] + addr + [0x00], read_len=len(blob))
    assert got == blob, f"read back {got!r} != {blob!r}"


@cocotb.test(timeout_time=TIMEOUT, timeout_unit="us")
async def test_vendor_write_only_reaches_flash(dut):
    """A write-only EXEC (WREN) completes its status stage and the opcode is
    seen on the SPI bus."""
    host, flash = await _bringup(dut)
    await _enumerate(host)
    flash.transactions.clear()

    await _spi(host, [WREN])          # no read-back
    await Timer(50, unit="us")
    assert WREN in [t.opcode for t in flash.transactions], \
        f"WREN not observed (opcodes={[hex(t.opcode) for t in flash.transactions]})"
