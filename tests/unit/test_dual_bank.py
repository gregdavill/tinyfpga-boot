import unittest
from tests.unit.util import simulate
from gateware.blocks.boot.dual_bank import DualBank


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
