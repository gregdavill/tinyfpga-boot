import unittest
from amaranth import *
from amaranth.lib import wiring, io

from gateware.blocks.qspi.controller import Controller, PortGroup
from gateware.blocks.qspi.spi_bridge import SpiBridge
from tests.unit.util import stream_get, stream_put, simulate


class TestSpiBridge(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads
            super().__init__()

        def elaborate(self, platform):
            m = Module()
            m.submodules.bridge = bridge = SpiBridge()
            m.submodules.qspi = qspi = Controller(self._pads)
            wiring.connect(m, bridge.qo, qspi.i)
            wiring.connect(m, bridge.qi, qspi.o)
            m.d.comb += qspi.divisor.eq(1)

            self.rx = bridge.rx
            self.tx = bridge.tx
            self.boot = bridge.boot
            return m

    def setUp(self):
        self.pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )
        self.dut = self.DUT(self.pads)

    async def _put(self, ctx, b):
        await stream_put(ctx, self.dut.rx, {"data": b, "first": 0, "last": 0})

    def test_read(self):
        """0x01 frame: 5 write bytes (0x0b + addr + dummy), then read 2 bytes."""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0010)  # io1=1 -> every GetX1 byte reads 0xFF
            await ctx.tick().repeat(3)

            # cmd, wlen=5 (LE), rlen=2 (LE), then the 5-byte write_string.
            for b in [0x01, 5, 0, 2, 0, 0x0b, 0x00, 0x10, 0x00, 0x00]:
                await self._put(ctx, b)

            r0 = await stream_get(ctx, dut.tx)
            r1 = await stream_get(ctx, dut.tx)
            self.assertEqual(r0["data"], 0xFF)
            self.assertEqual(r1["data"], 0xFF)
            self.assertEqual(r0["first"], 1)
            self.assertEqual(r0["last"], 0)
            self.assertEqual(r1["last"], 1)

        simulate(dut, testbench)

    def test_write_only(self):
        """0x01 frame with rlen=0 completes and returns to idle (no tx)."""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)
            await ctx.tick().repeat(3)

            # WREN-style: cmd, wlen=1, rlen=0, opcode 0x06.
            for b in [0x01, 1, 0, 0, 0, 0x06]:
                await self._put(ctx, b)

            # Give the transaction time to release CS and return to idle,
            # then a fresh command must still be accepted.
            await ctx.tick().repeat(200)
            ctx.set(dut.rx.p.data, 0x01)
            ctx.set(dut.rx.valid, 1)
            accepted = False
            for _ in range(50):
                if ctx.get(dut.rx.ready):
                    accepted = True
                    break
                await ctx.tick()
            self.assertTrue(accepted)

        simulate(dut, testbench)

    def test_empty_frame(self):
        """A 0x01 frame with wlen=0 and rlen=0 is a CS-only no-op: it toggles
        CS via RELEASE and returns to idle without emitting any tx bytes."""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)
            await ctx.tick().repeat(3)

            # cmd, wlen=0, rlen=0 — nothing shifted, nothing read.
            for b in [0x01, 0, 0, 0, 0]:
                await self._put(ctx, b)

            # Must release CS and return to idle, accepting a fresh command.
            await ctx.tick().repeat(50)
            ctx.set(dut.rx.p.data, 0x01)
            ctx.set(dut.rx.valid, 1)
            accepted = False
            for _ in range(50):
                if ctx.get(dut.rx.ready):
                    accepted = True
                    break
                await ctx.tick()
            self.assertTrue(accepted)

        simulate(dut, testbench)

    def test_boot(self):
        """A lone 0x00 byte pulses `boot`."""
        dut = self.dut

        async def testbench(ctx):
            await ctx.tick().repeat(3)
            ctx.set(dut.rx.p.data, 0x00)
            ctx.set(dut.rx.valid, 1)
            # `boot` is combinational on (CMD_IDLE & rx.valid & data==0).
            self.assertEqual(ctx.get(dut.boot), 1)

        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()
