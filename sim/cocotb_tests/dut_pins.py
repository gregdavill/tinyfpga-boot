"""Aliases for the auto-generated Verilog port names.

Amaranth derives top-level port names from the resource hierarchy:
``<resource>_<number>__<subsignal>__io``. That gets verbose.
expose a small adapter for tests + models.

package pads are `inout` because `IOPort` is bidirectional.
SB_IO behavioural model handles direction internally.
"""

from dataclasses import dataclass


@dataclass
class DutPins:
    clk16:   object   # 16 MHz input clock the PLL multiplies up
    usb_d_p: object
    usb_d_n: object
    usb_pullup: object
    spi_cs:  object
    spi_clk: object
    spi_dq:  object   # 4-bit


def attach(dut) -> DutPins:
    return DutPins(
        clk16      = dut.clk16_0__io,
        usb_d_p    = dut.usb_0__d_p__io,
        usb_d_n    = dut.usb_0__d_n__io,
        usb_pullup = dut.usb_0__pullup__io,
        spi_cs     = dut.spi_flash_4x_0__cs__io,
        spi_clk    = dut.spi_flash_4x_0__clk__io,
        spi_dq     = dut.spi_flash_4x_0__dq__io,
    )


def release(signal):
    """Set a (possibly multi-bit) inout signal to all-Z.

    cocotb 2.0's LogicArray constructor refuses width-mismatched
    strings - `"z"` is fine for a 1-bit pad but a 4-bit pad needs
    `"zzzz"`. We figure out the width from the handle so callers
    can stay agnostic."""
    try:
        width = len(signal)
    except TypeError:
        width = 1
    signal.value = "z" * width
