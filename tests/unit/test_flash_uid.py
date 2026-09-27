import unittest
from amaranth import *
from amaranth.lib import wiring, io

from gateware.blocks.qspi.controller import Controller, PortGroup, Mode
from gateware.blocks.serial.flash_uid import FlashUID
from tests.unit.util import stream_get, stream_put, simulate


class TestStride(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads
            super().__init__()

        def elaborate(self, platform) -> Module:
            m = Module()

            m.submodules.uuid = uuid = FlashUID()
            m.submodules.qspi = qspi = Controller(self._pads)

            wiring.connect(m, uuid.o, qspi.i)
            wiring.connect(m, uuid.i, qspi.o)

            self.uuid = uuid
            self.data = uuid.data
            self.done = uuid.done

            m.d.sync += uuid.req.eq(1)
            m.d.comb += qspi.divisor.eq(1)

            return m

    def test_basic(self):
        pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )

        dut = self.DUT(pads)
        got = bytearray()

        async def consume(ctx):
            ctx.set(dut.data.ready, 1)
            for _ in range(600):
                if ctx.get(dut.done):
                    break
                if ctx.get(dut.data.valid):
                    got.append(ctx.get(dut.data.p.data))
                await ctx.tick()

        simulate(dut, consume)
        # The QSPI bus is undriven here, so the bytes are don't-care, but
        # exactly `uid_bytes` of them must stream out before `done`.
        self.assertEqual(len(got), 8)


if __name__ == "__main__":
    unittest.main()
