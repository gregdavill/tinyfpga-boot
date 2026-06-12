"""Unconditional stay source.

For boards with no usable default stay control. e.g. the TinyFPGA BX, whose only
button is wired to the FPGA reset. Keeps the bootloader active on every power-on
so it always enumerates over USB instead of auto-booting the application.
"""

from amaranth import Module

from . import StaySource


class AlwaysStaySource(StaySource):
    needs_flash = False

    def elaborate(self, platform):
        m = Module()
        # Purely combinational: no flash, always "ready", always stay.
        m.d.comb += self.stay.eq(1)
        m.d.comb += self.done.eq(1)
        return m
