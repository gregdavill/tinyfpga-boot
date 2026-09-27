import unittest
from amaranth.lib import io

from gateware.blocks.qspi.controller import Controller, Mode
from gateware.blocks.qspi.ports import PortGroup
from tests.unit.util import stream_get, stream_put, simulate


class TestController(unittest.TestCase):

    def setUp(self):
        self.pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )
        self.dut = Controller(self.pads)

    def test_putx1(self):
        dut = self.dut
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 1, 'data': 0xA5, 'mode': Mode.PutX1})
        simulate(dut, testbench)

    def test_putx4(self):
        dut = self.dut
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            await ctx.tick().repeat(3)
            for v in [0x55, 0xAA, 0xFF]:
                await stream_put(ctx, dut.i, {'chip': 1, 'data': v, 'mode': Mode.PutX4})
        simulate(dut, testbench)

    def test_getx1_ones(self):
        dut, pads = self.dut, self.pads
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            ctx.set(pads.dq.i, 0b0010)  # io1=1
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 1, 'data': 0, 'mode': Mode.GetX1})
            result = await stream_get(ctx, dut.o)
            self.assertEqual(result['data'], 0xFF)
        simulate(dut, testbench)

    def test_getx1_zeros(self):
        dut, pads = self.dut, self.pads
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            ctx.set(pads.dq.i, 0b0000)  # io1=0
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 1, 'data': 0, 'mode': Mode.GetX1})
            result = await stream_get(ctx, dut.o)
            self.assertEqual(result['data'], 0x00)
        simulate(dut, testbench)

    def test_getx4(self):
        dut, pads = self.dut, self.pads
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            ctx.set(pads.dq.i, 0b0101)  # Cat(io0,io1,io2,io3) = 1,0,1,0 = nibble 0x5
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 1, 'data': 0, 'mode': Mode.GetX4})
            result = await stream_get(ctx, dut.o)
            self.assertEqual(result['data'], 0x55)
        simulate(dut, testbench)

    def test_swap(self):
        dut, pads = self.dut, self.pads
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            ctx.set(pads.dq.i, 0b0010)  # io1=1
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 1, 'data': 0xA5, 'mode': Mode.Swap})
            result = await stream_get(ctx, dut.o)
            self.assertEqual(result['data'], 0xFF)
        simulate(dut, testbench)

    def test_back_to_back_getx1(self):
        dut, pads = self.dut, self.pads
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            ctx.set(pads.dq.i, 0b0010)  # io1=1
            await ctx.tick().repeat(3)
            for _ in range(3):
                await stream_put(ctx, dut.i, {'chip': 1, 'data': 0, 'mode': Mode.GetX1})
                result = await stream_get(ctx, dut.o)
                self.assertEqual(result['data'], 0xFF)
        simulate(dut, testbench)

    def test_dummy_completes(self):
        dut = self.dut
        async def testbench(ctx):
            ctx.set(dut.divisor, 1)
            await ctx.tick().repeat(3)
            await stream_put(ctx, dut.i, {'chip': 0, 'data': 0, 'mode': Mode.Dummy})
        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()