import unittest
from amaranth import Elaboratable, Module
from amaranth.lib import wiring as _wiring, io

from gateware.blocks.qspi.controller import Controller, PortGroup
from gateware.staysource.write_enable import WriteEnableStaySource
from tests.unit.util import simulate


class TestWriteEnableStaySource(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads

        def elaborate(self, platform):
            m = Module()
            m.submodules.src = src = WriteEnableStaySource()
            m.submodules.qspi = qspi = Controller(self._pads)
            _wiring.connect(m, src.o, qspi.i)
            _wiring.connect(m, src.i, qspi.o)
            m.d.comb += qspi.divisor.eq(1)
            self.src = src
            return m

    def _verdict(self, wel_high):
        pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )
        dut = self.DUT(pads)
        out = {}

        async def tb(ctx):
            # GetX1 samples io1; a constant level makes the status byte 0xFF
            # (WEL set) or 0x00 (WEL clear).
            ctx.set(pads.dq.i, 0b0010 if wel_high else 0b0000)
            ctx.set(dut.src.req, 1)
            for _ in range(2000):
                await ctx.tick()
                if ctx.get(dut.src.done):
                    out["stay"] = ctx.get(dut.src.stay)
                    return

        simulate(dut, tb)
        self.assertIn("stay", out, "reader never asserted done")
        return out["stay"]

    def test_wel_set_stays(self):
        self.assertEqual(self._verdict(wel_high=True), 1)

    def test_wel_clear_boots(self):
        self.assertEqual(self._verdict(wel_high=False), 0)


if __name__ == "__main__":
    unittest.main()
