import unittest
from tests.unit.util import stream_get, simulate
from gateware.blocks.qspi.controller import Mode
from gateware.blocks.qspi.flash_wake import FlashWake, MODE_BIT_RESET, RELEASE_POWERDOWN


class TestFlashWake(unittest.TestCase):
    def test_emits_mbr_then_release_then_settles(self):
        """The reader clocks 0xFF, releases CS, clocks 0xAB, releases CS, waits,
        then asserts `done`."""
        dut = FlashWake(settle_cycles=8)
        seen = []
        result = {}

        async def collect(ctx):
            # The QSPI command tokens appear on `o`; capture (chip, mode, data).
            for _ in range(4):
                tok = await stream_get(ctx, dut.o)
                seen.append((tok["chip"], tok["mode"], tok["data"]))

        async def drive(ctx):
            ctx.set(dut.req, 1)
            for _ in range(80):
                if ctx.get(dut.done):
                    break
                await ctx.tick()
            result["done"] = ctx.get(dut.done)

        simulate(dut, collect, drive)

        self.assertEqual(seen[0], (1, Mode.PutX1, MODE_BIT_RESET))
        self.assertEqual(seen[1][0], 0)                       # CS# released
        self.assertEqual(seen[2], (1, Mode.PutX1, RELEASE_POWERDOWN))
        self.assertEqual(seen[3][0], 0)                       # CS# released
        self.assertTrue(result["done"])


if __name__ == "__main__":
    unittest.main()
