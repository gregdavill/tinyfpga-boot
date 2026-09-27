"""End-to-end tests for auto-boot (FS DUT, BACKEND=uf2, AUTOBOOT=1).

Build with `make AUTOBOOT=1`: the DUT is elaborated with the button + WEL stay
sources enabled. On power-on the bootloader reads the serial source, probes slot
1, and reads the flash status register, then decides: stay (enumerate over USB)
or reboot into the slot-1 app.

The decision is the OR of every stay source's veto:
  * NoValidAppStaySource  -- slot 1 lacks the iCE40 sync word (0x7EAA997E)
  * WriteEnableStaySource -- the flash WEL latch is set
  * ButtonStaySource      -- the boards button is held

`test_auto_boots_into_app` covers the all-clear case (auto-boot); the
`*_stays` tests cover each veto in isolation.
"""

import cocotb
from cocotb.triggers import Timer
from cocotb.clock import Clock

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


APP_OFFSET = 0x40000   # SLOT1_OFFSET

# A minimal iCE40 image header: a comment byte run followed by the 0x7EAA997E
# sync word, so NoValidAppStaySource recognises slot 1 as a valid app.
VALID_APP_HEADER = bytes(
    [0xFF, 0x00, 0x00, 0xFF, 0x7E, 0xAA, 0x99, 0x7E, 0x51, 0x00, 0x01, 0x05])


async def _powerup(dut, *, button_pressed=False, slot1_valid=True, wel=False):
    """Bring the DUT out of reset with the requested stay-source conditions and
    return (host, flash). 
    """
    flash = SPIFlashModel(dut, uid=b"\x01\x02\x03\x04\x05\x06\x07\x08")
    if slot1_valid:
        flash.memory[APP_OFFSET:APP_OFFSET + len(VALID_APP_HEADER)] = VALID_APP_HEADER
    if wel:
        flash.status |= 0x02   # WEL survives a reconfigure into the bootloader
    cocotb.start_soon(flash.run())

    host = USBHost(dut)
    # Active-low PinsN button: pad high => released, pad low => held/pressed.
    dut.button_0__io.value = 0 if button_pressed else 1
    cocotb.start_soon(Clock(host.pins.clk16, 20834, unit="ps").start())
    await host._release()
    return host, flash


async def _await_outcome(dut, host, *, max_us=8000):
    """Race the two mutually-exclusive power-on outcomes: a warmboot pulse
    (auto-boot) or the USB pullup asserting (stayed and enumerating)."""
    for _ in range(max_us):
        await Timer(1, unit="us")
        try:
            if int(dut.reconfigure.boot.value) == 1:
                return "boot"
        except (ValueError, AttributeError):
            pass
        try:
            if int(host.pins.usb_pullup.value) == 1:
                return "stay"
        except (ValueError, AttributeError):
            pass
    return None


@cocotb.test(timeout_time=8000, timeout_unit="us")
async def test_auto_boots_into_app(dut):
    """All sources clear: valid slot-1 image, WEL clear, button released ->
    the bootloader reboots straight into the app."""
    host, flash = await _powerup(dut)

    outcome = await _await_outcome(dut, host)
    assert outcome == "boot", f"expected auto-boot (SB_WARMBOOT), got {outcome!r}"

    # The decision must actually have consulted flash: the slot-1 probe
    # (0x0B fast read) and the status-register read (0x05) for WEL.
    ops = [t.opcode for t in flash.transactions]
    assert 0x0B in ops, f"slot 1 never probed (opcodes={[hex(o) for o in ops]})"
    assert 0x05 in ops, f"status register never read (opcodes={[hex(o) for o in ops]})"
    read = next(t for t in flash.transactions if t.opcode == 0x0B)
    assert read.address == APP_OFFSET, \
        f"probed 0x{read.address:x}, expected slot-1 0x{APP_OFFSET:x}"


@cocotb.test(timeout_time=8000, timeout_unit="us")
async def test_no_valid_app_stays(dut):
    """Slot 1 erased (no sync word) -> NoValidAppStaySource vetoes -> the
    bootloader stays resident and enumerates instead of warmbooting."""
    host, flash = await _powerup(dut, slot1_valid=False)

    outcome = await _await_outcome(dut, host)
    assert outcome == "stay", f"expected stay (no valid app), got {outcome!r}"

    # It must have looked: the slot-1 probe is what found no bitstream.
    ops = [t.opcode for t in flash.transactions]
    assert 0x0B in ops, f"slot 1 never probed (opcodes={[hex(o) for o in ops]})"


@cocotb.test(timeout_time=8000, timeout_unit="us")
async def test_write_enable_latch_stays(dut):
    """Valid app present, but the volatile WEL latch is set -> stay. This is the path an application uses
    to re-enter the bootloader: issue WREN, then trigger a reconfigure."""
    host, flash = await _powerup(dut, wel=True)

    outcome = await _await_outcome(dut, host)
    assert outcome == "stay", f"expected stay (WEL set), got {outcome!r}"

    ops = [t.opcode for t in flash.transactions]
    assert 0x05 in ops, f"status register never read (opcodes={[hex(o) for o in ops]})"


@cocotb.test(timeout_time=8000, timeout_unit="us")
async def test_button_held_stays(dut):
    """Valid app present and WEL clear, but the button is held ->
    ButtonStaySource vetoes -> stay."""
    host, _ = await _powerup(dut, button_pressed=True)

    outcome = await _await_outcome(dut, host)
    assert outcome == "stay", f"expected stay (button held), got {outcome!r}"
