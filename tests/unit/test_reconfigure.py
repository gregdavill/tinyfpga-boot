import unittest
from types import SimpleNamespace

from amaranth import Signal

from gateware.blocks.boot.reconfigure import OpenDrainReconfigure
from tests.unit.util import simulate


class _Platform:
    def __init__(self):
        self.port = SimpleNamespace(o=Signal())

    def request(self, name, dir):
        return self.port


class _DUT(OpenDrainReconfigure):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.platform = _Platform()

    def elaborate(self, platform):
        return super().elaborate(self.platform)


class TestOpenDrainReconfigure(unittest.TestCase):
    async def _arm(self, ctx, dut):
        ctx.set(dut.arm, 1)
        await ctx.tick()
        ctx.set(dut.arm, 0)

    async def _cycles_until(self, ctx, sig, limit=100):
        for n in range(limit):
            if ctx.get(sig):
                return n
            await ctx.tick()
        self.fail("signal never asserted")

    def test_holds_phy_reset_before_programn(self):
        dut = _DUT(idle_cycles=4, phy_reset_cycles=8)

        async def tb(ctx):
            await self._arm(ctx, dut)
            await self._cycles_until(ctx, dut.phy_reset)
            self.assertEqual(ctx.get(dut.boot), 0)
            self.assertEqual(await self._cycles_until(ctx, dut.boot), 8)
            self.assertEqual(ctx.get(dut.platform.port.o), 1)
            self.assertEqual(ctx.get(dut.phy_reset), 1)

            # Both stay high: the PHY must not come back before the FPGA goes.
            await ctx.tick().repeat(4)
            self.assertEqual(ctx.get(dut.boot), 1)
            self.assertEqual(ctx.get(dut.phy_reset), 1)

        simulate(dut, tb)

    def test_activity_defers_hold_but_cannot_abort_it(self):
        dut = _DUT(idle_cycles=4, phy_reset_cycles=8)

        async def tb(ctx):
            await self._arm(ctx, dut)
            ctx.set(dut.activity, 1)
            await ctx.tick().repeat(10)
            self.assertEqual(ctx.get(dut.phy_reset), 0)

            ctx.set(dut.activity, 0)
            await self._cycles_until(ctx, dut.phy_reset)
            ctx.set(dut.activity, 1)
            self.assertEqual(await self._cycles_until(ctx, dut.boot), 8)

        simulate(dut, tb)

    def test_no_hold_without_phy_reset_cycles(self):
        dut = _DUT(idle_cycles=4)

        async def tb(ctx):
            await self._arm(ctx, dut)
            await self._cycles_until(ctx, dut.boot)
            self.assertEqual(ctx.get(dut.platform.port.o), 1)

        simulate(dut, tb)

    def test_stays_idle_until_armed(self):
        dut = _DUT(idle_cycles=4, phy_reset_cycles=8)

        async def tb(ctx):
            await ctx.tick().repeat(32)
            self.assertEqual(ctx.get(dut.phy_reset), 0)
            self.assertEqual(ctx.get(dut.boot), 0)

        simulate(dut, tb)


if __name__ == "__main__":
    unittest.main()
