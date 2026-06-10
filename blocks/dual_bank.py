"""Dual-bank (FLASH, or QSPI RAM) control

The ECPBreaker uses the shared QSPI bus to either drive a config FLASH or an
APS6404L QSPI PSRAM. An external buffer and DFF latches `cfg_ctrl` on every CS
*rising* edge. This latch muxes the CS line (0 = FLASH, 1 = RAM) and
remains even when the FPGA reconfigures. The DFF resets to FLASH on POR.

`DualBank` is the small helper:

  * drives `cfg_ctrl` from `bank`
  * tracks how long CS has been continuously asserted. `tcem_expired` flags this
    so we can break a burst before a PSRAM's tCEM is violated.

Upstream logic is responsible for sending a dummy CS pulse to latch the mux value.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class DualBank(wiring.Component):
    def __init__(self, *, max_cs_cycles):
        """`max_cs_cycles`: assert `tcem_expired` once CS has been continuously
        asserted for this many sync cycles. 
        Set below tCEM, ~400 cycles ≈ 6.7 us at 60 MHz"""
        if max_cs_cycles < 1:
            raise ValueError("max_cs_cycles must be at least 1")
        self._max = max_cs_cycles
        super().__init__({
            "bank":         In(1),    # 0 = FLASH, 1 = RAM
            "cs_open":      In(1),    # high while the writer holds CS asserted
            "tcem_expired": Out(1),   # CS has been open >= max_cs_cycles
            "cfg_ctrl_o":   Out(1),   # -> the cfg_ctrl pad
        })

    def elaborate(self, platform):
        m = Module()

        m.d.comb += self.cfg_ctrl_o.eq(self.bank)

        # Count sync cycles of continuous CS-active; reset the moment CS drops.
        cnt = Signal(range(self._max + 1))
        with m.If(self.cs_open):
            with m.If(cnt != self._max):
                m.d.sync += cnt.eq(cnt + 1)
        with m.Else():
            m.d.sync += cnt.eq(0)

        m.d.comb += self.tcem_expired.eq(cnt == self._max)

        return m


# Test cases
import unittest
from .test_util import simulate


class TestDualBank(unittest.TestCase):
    def test_cfg_ctrl_follows_bank(self):
        dut = DualBank(max_cs_cycles=5)

        async def tb(ctx):
            ctx.set(dut.bank, 0)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.cfg_ctrl_o), 0)
            ctx.set(dut.bank, 1)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.cfg_ctrl_o), 1)

        simulate(dut, tb)

    def test_tcem_expires_and_resets(self):
        dut = DualBank(max_cs_cycles=5)

        async def tb(ctx):
            # CS held open: counter climbs to the threshold, then latches.
            ctx.set(dut.cs_open, 1)
            for _ in range(5):
                self.assertEqual(ctx.get(dut.tcem_expired), 0)
                await ctx.tick()
            self.assertEqual(ctx.get(dut.tcem_expired), 1)

            # Dropping CS resets the counter (tCEM window restarts).
            ctx.set(dut.cs_open, 0)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.tcem_expired), 0)

        simulate(dut, tb)


if __name__ == "__main__":
    unittest.main()
