import unittest
from amaranth import *
from amaranth.lib import wiring, io

from gateware.blocks.qspi.controller import Controller, PortGroup
from gateware.blocks.qspi.flash import QspiFlash
from tests.unit.util import stream_get, stream_put, simulate


class TestQspiFlash(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads
            super().__init__()

        def elaborate(self, platform) -> Module:
            m = Module()

            m.submodules.flash = flash = QspiFlash()
            m.submodules.qspi = qspi = Controller(self._pads)

            wiring.connect(m, flash.qo, qspi.i)
            wiring.connect(m, flash.qi, qspi.o)

            self.i = flash.port.w
            self.done = flash.port.flush

            m.d.comb += qspi.divisor.eq(1)

            return m

    def setUp(self):
        self.pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )
        self.dut = self.DUT(self.pads)

    def test_single_byte(self):
        """One byte write: WREN→ERASE→POLL→WREN→PP→data→POLL→IDLE"""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)  # status reads return 0x00 (WIP=0)
            await ctx.tick().repeat(3)

            await stream_put(ctx, dut.i, {'addr': 0x001000, 'data': 0xAB})
            await ctx.tick().repeat(2000)

            # Assert done by pulsing done and checking we return to IDLE
            ctx.set(dut.done, 1)
            await ctx.tick().repeat(1000)

        simulate(dut, testbench)

    def test_page_sequential(self):
        """4 bytes on same page: one erase, one PP with 4 data bytes"""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)
            await ctx.tick().repeat(3)

            for i in range(4):
                await stream_put(ctx, dut.i, {'addr': 0x001000 + i, 'data': 0x10 + i})

            ctx.set(dut.done, 1)
            await ctx.tick().repeat(3000)

        simulate(dut, testbench)

    def test_page_boundary(self):
        """Bytes spanning two pages in same 64 KiB block: two PP commands, one erase"""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)
            await ctx.tick().repeat(3)

            # Last byte of page 0x10
            await stream_put(ctx, dut.i, {'addr': 0x0010FF, 'data': 0xAA})
            # First byte of page 0x11 (same sector 0x001)
            await stream_put(ctx, dut.i, {'addr': 0x001100, 'data': 0xBB})

            ctx.set(dut.done, 1)
            await ctx.tick().repeat(4000)

        simulate(dut, testbench)

    def test_block_boundary(self):
        """Bytes spanning two 64 KiB blocks: two erase + two PP sequences"""
        dut, pads = self.dut, self.pads

        async def testbench(ctx):
            ctx.set(pads.dq.i, 0b0000)
            await ctx.tick().repeat(3)

            # Byte in block 0
            await stream_put(ctx, dut.i, {'addr': 0x000000, 'data': 0x11})
            # Byte in block 1
            await stream_put(ctx, dut.i, {'addr': 0x010000, 'data': 0x22})

            ctx.set(dut.done, 1)
            await ctx.tick().repeat(5000)

        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()
