import unittest
from amaranth import Module
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out, connect, flipped
from amaranth.sim import Simulator

from gateware.blocks.qspi.arbiter import Arbiter, HoldArbiter
from tests.unit.util import simulate


def _run_comb(dut, tb):
    """Run a testbench against a clockless (combinational) DUT."""
    sim = Simulator(dut)
    sim.add_testbench(tb)
    sim.run()


_QO = data.StructLayout({"chip": 2, "data": 8})
_QI = data.StructLayout({"data": 8})
_BUS = wiring.Signature({
    "qo": Out(stream.Signature(_QO)),
    "qi": In(stream.Signature(_QI)),
})


class _BusDUT(wiring.Component):
    """Two producers -> arbiter -> one consumer, all of the qo/qi bus signature,
    with the ports surfaced for the testbench to drive/observe."""
    def __init__(self, arb):
        self._n = arb._n
        super().__init__({
            "prod": In(_BUS).array(self._n),   # driven by the tb as producers
            "cons": Out(_BUS),                 # observed by the tb as consumer
            "active": In(self._n),
            "grant": Out(range(self._n + 1)),
        })
        self.arb = arb

    def elaborate(self, platform):
        m = Module()
        m.submodules.arb = arb = self.arb
        for i in range(self._n):
            connect(m, flipped(self.prod[i]), arb.inp[i])
        connect(m, arb.out, flipped(self.cons))
        m.d.comb += arb.active.eq(self.active)
        m.d.comb += self.grant.eq(arb.grant)
        return m


class TestArbiter(unittest.TestCase):
    def test_priority_and_forward(self):
        """Lowest active index wins; its qo forwards to the consumer and the
        consumer's ready backpressures only the granted producer."""
        dut = _BusDUT(Arbiter(_BUS, 2))

        async def tb(ctx):
            ctx.set(dut.cons.qo.ready, 1)
            ctx.set(dut.active, 0b11)          # both request; producer 0 wins
            ctx.set(dut.prod[0].qo.valid, 1)
            ctx.set(dut.prod[0].qo.payload.data, 0xA1)
            ctx.set(dut.prod[1].qo.valid, 1)
            ctx.set(dut.prod[1].qo.payload.data, 0xB2)
            await ctx.delay(1e-9)
            self.assertEqual(ctx.get(dut.grant), 0)
            self.assertEqual(ctx.get(dut.cons.qo.valid), 1)
            self.assertEqual(ctx.get(dut.cons.qo.payload.data), 0xA1)
            self.assertEqual(ctx.get(dut.prod[0].qo.ready), 1)
            self.assertEqual(ctx.get(dut.prod[1].qo.ready), 0)

        _run_comb(dut, tb)

    def test_response_routes_to_granted(self):
        """The consumer's qi (response) is delivered to the granted producer
        only; others see valid=0."""
        dut = _BusDUT(Arbiter(_BUS, 2))

        async def tb(ctx):
            ctx.set(dut.active, 0b10)          # only producer 1 active
            ctx.set(dut.prod[1].qo.valid, 1)
            ctx.set(dut.cons.qi.valid, 1)
            ctx.set(dut.cons.qi.payload.data, 0x5C)
            ctx.set(dut.prod[1].qi.ready, 1)
            await ctx.delay(1e-9)
            self.assertEqual(ctx.get(dut.grant), 1)
            self.assertEqual(ctx.get(dut.prod[1].qi.valid), 1)
            self.assertEqual(ctx.get(dut.prod[1].qi.payload.data), 0x5C)
            self.assertEqual(ctx.get(dut.prod[0].qi.valid), 0)
            self.assertEqual(ctx.get(dut.cons.qi.ready), 1)

        _run_comb(dut, tb)

    def test_combinational_no_hold(self):
        """The base arbiter does not latch: the grant follows `active` the same
        cycle and releases to idle as soon as the producer goes quiet."""
        dut = _BusDUT(Arbiter(_BUS, 2))

        async def tb(ctx):
            ctx.set(dut.active, 0b01)
            await ctx.delay(1e-9)
            self.assertEqual(ctx.get(dut.grant), 0)
            ctx.set(dut.active, 0)             # quiet → immediately idle
            await ctx.delay(1e-9)
            self.assertEqual(ctx.get(dut.grant), 2)   # n=2

        _run_comb(dut, tb)


class TestHoldArbiter(unittest.TestCase):
    def test_grant_latches_and_releases(self):
        """Grant holds while active, and releases `idle_hold` cycles after the
        producer goes idle."""
        dut = _BusDUT(HoldArbiter(_BUS, 2, idle_hold=2))

        async def tb(ctx):
            ctx.set(dut.active, 0b01)
            ctx.set(dut.prod[0].qo.valid, 1)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.grant), 0)
            # Go idle; grant should persist through the idle_hold window.
            ctx.set(dut.active, 0)
            ctx.set(dut.prod[0].qo.valid, 0)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.grant), 0, "released too early")
            await ctx.tick().repeat(3)
            self.assertEqual(ctx.get(dut.grant), 2, "never released")  # n=2 → idle

        simulate(dut, tb)

    def test_holds_priority_routing(self):
        """Held grant still routes qo/qi like the base arbiter."""
        dut = _BusDUT(HoldArbiter(_BUS, 2, idle_hold=2))

        async def tb(ctx):
            ctx.set(dut.cons.qo.ready, 1)
            ctx.set(dut.active, 0b10)
            ctx.set(dut.prod[1].qo.valid, 1)
            ctx.set(dut.prod[1].qo.payload.data, 0x7E)
            await ctx.tick()
            self.assertEqual(ctx.get(dut.grant), 1)
            self.assertEqual(ctx.get(dut.cons.qo.payload.data), 0x7E)
            self.assertEqual(ctx.get(dut.prod[1].qo.ready), 1)

        simulate(dut, tb)


if __name__ == "__main__":
    unittest.main()
