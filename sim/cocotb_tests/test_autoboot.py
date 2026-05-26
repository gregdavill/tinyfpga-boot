"""End-to-end test for auto-boot (FS DUT, BACKEND=uf2, AUTOBOOT=1).

Build with `make AUTOBOOT=1`: the DUT is elaborated with the button + WEL stay
sources enabled. On power-on the bootloader reads the serial source, probes slot 1, 
and reads the flash status register, then decides: stay (enumerate) or reboot
into the app.
"""

import cocotb
from cocotb.triggers import Timer
from cocotb.clock import Clock

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


APP_OFFSET = 0x40000   # SLOT1_OFFSET


@cocotb.test(timeout_time=8000, timeout_unit="us")
async def test_auto_boots_into_app(dut):
    flash = SPIFlashModel(dut, uid=b"\x01\x02\x03\x04\x05\x06\x07\x08")
    # Program slot 1 with an iCE40 bitstream header (comment + 0x7EAA997E sync)
    # so NoValidAppStaySource recognises an app; leave WEL clear so
    # WriteEnableStaySource doesn't veto.
    flash.memory[APP_OFFSET:APP_OFFSET + 12] = bytes(
        [0xFF, 0x00, 0x00, 0xFF, 0x7E, 0xAA, 0x99, 0x7E, 0x51, 0x00, 0x01, 0x05])
    cocotb.start_soon(flash.run())

    host = USBHost(dut)
    # Release the button (active-low PinsN: pad high => not pressed). Drive it
    # to a defined level so the synchronized input isn't X.
    dut.button_0__io.value = 1
    cocotb.start_soon(Clock(host.pins.clk16, 20834, unit="ps").start())
    await host._release()

    # Race the two mutually-exclusive outcomes: a warmboot pulse (auto-boot)
    # or the USB pullup asserting (stayed and enumerating).
    outcome = None
    for _ in range(8000):
        await Timer(1, unit="us")
        try:
            if int(dut.reconfigure.boot.value) == 1:
                outcome = "boot"
                break
        except (ValueError, AttributeError):
            pass
        try:
            if int(host.pins.usb_pullup.value) == 1:
                outcome = "stay"
                break
        except (ValueError, AttributeError):
            pass

    assert outcome == "boot", f"expected auto-boot (SB_WARMBOOT), got {outcome!r}"

    # The decision must actually have consulted flash: the slot-1 probe
    # (0x0B fast read) and the status-register read (0x05) for WEL.
    ops = [t.opcode for t in flash.transactions]
    assert 0x0B in ops, f"slot 1 never probed (opcodes={[hex(o) for o in ops]})"
    assert 0x05 in ops, f"status register never read (opcodes={[hex(o) for o in ops]})"
    read = next(t for t in flash.transactions if t.opcode == 0x0B)
    assert read.address == APP_OFFSET, \
        f"probed 0x{read.address:x}, expected slot-1 0x{APP_OFFSET:x}"
