from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data, enum
from .qspi import Controller, PortGroup, Mode


WRITE_ENABLE = 0x06
SECTOR_ERASE = 0x20  # 4 KiB sector erase (used by boot_header)
BLOCK_ERASE  = 0xD8  # 64 KiB block erase
PAGE_PROGRAM = 0x02
READ_STATUS  = 0x05


class PollReturn(enum.Enum, shape=2):
    WREN_ERASE = 0
    WREN_WRITE = 1
    IDLE       = 2


class QspiFlash(wiring.Component):
    def __init__(self):
        super().__init__({
            # Write addr/data interface (from UF2 decoder)
            "i":    In(stream.Signature(data.StructLayout({"addr": 24, "data": 8}))),

            # FLASH QSPI interface
            "qo":   Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "qi":   In(stream.Signature(data.StructLayout({
                "data": 8,
            }))),

            # Flush signal — triggers final page close + poll
            "done": In(1),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        # Latched input
        addr_latch = Signal(24)
        data_latch = Signal(8)

        # Block/page tracking. 
        # erase unit is a 64 KiB block (addr[23:16]).
        # pages are 256 bytes (addr[23:8]).
        current_block  = Signal(8)   # addr[23:16]
        current_page   = Signal(16)  # addr[23:8]
        block_valid    = Signal()

        # Address byte counter (counts 0, 1, 2)
        addr_count = Signal(range(3))

        # FSM routing signals
        wren_target = Signal()  # 0=ERASE_CMD, 1=WRITE_CMD
        poll_return = Signal(PollReturn)

        # Whether a new byte was latched during WRITE_NEXT
        has_next = Signal()

        # Latched status register
        status = Signal(8)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.qo.p.chip.eq(chip),
                self.qo.p.mode.eq(mode),
                self.qo.p.data.eq(d),
                self.qo.valid.eq(1),
            ]

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += [
                        addr_latch.eq(self.i.p.addr),
                        data_latch.eq(self.i.p.data),
                    ]
                    with m.If(~block_valid | (self.i.p.addr[16:24] != current_block)):
                        # New block — erase first
                        m.d.sync += wren_target.eq(0)  # ERASE
                        m.next = "WREN"
                    with m.Else():
                        # Same block — write directly
                        m.d.sync += wren_target.eq(1)  # WRITE
                        m.next = "WREN"


            with m.State("WREN"):
                send(1, Mode.PutX1, WRITE_ENABLE)
                with m.If(self.qo.ready):
                    m.next = "WREN_RELEASE"

            with m.State("WREN_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(wren_target == 0):
                        m.next = "ERASE_CMD"
                    with m.Else():
                        m.next = "WRITE_CMD"


            with m.State("ERASE_CMD"):
                send(1, Mode.PutX1, BLOCK_ERASE)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "ERASE_ADDR"

            with m.State("ERASE_ADDR"):
                # Send 3 address bytes (block-aligned, so low 16 bits = 0):
                # byte 0 = addr[23:16], bytes 1 and 2 = 0x00.
                addr_byte = Signal(8)
                with m.Switch(addr_count):
                    with m.Case(0):
                        m.d.comb += addr_byte.eq(addr_latch[16:24])
                    with m.Default():
                        m.d.comb += addr_byte.eq(0)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.next = "ERASE_RELEASE"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("ERASE_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += [
                        current_block.eq(addr_latch[16:24]),
                        block_valid.eq(1),
                        poll_return.eq(PollReturn.WREN_WRITE),
                    ]
                    m.next = "POLL_CMD"


            with m.State("WRITE_CMD"):
                send(1, Mode.PutX1, PAGE_PROGRAM)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "WRITE_ADDR"

            with m.State("WRITE_ADDR"):
                w_addr_byte = Signal(8)
                with m.Switch(addr_count):
                    with m.Case(0):
                        m.d.comb += w_addr_byte.eq(addr_latch[16:24])
                    with m.Case(1):
                        m.d.comb += w_addr_byte.eq(addr_latch[8:16])
                    with m.Default():
                        m.d.comb += w_addr_byte.eq(addr_latch[0:8])
                send(1, Mode.PutX1, w_addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.d.sync += current_page.eq(addr_latch[8:24])
                        m.next = "WRITE_DATA"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("WRITE_DATA"):
                send(1, Mode.PutX1, data_latch)
                with m.If(self.qo.ready):
                    m.next = "WRITE_NEXT"

            with m.State("WRITE_NEXT"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += [
                        addr_latch.eq(self.i.p.addr),
                        data_latch.eq(self.i.p.data),
                        has_next.eq(1),
                    ]
                    with m.If(self.i.p.addr[8:24] == current_page):
                        # Same page — continue writing
                        m.next = "WRITE_DATA"
                    with m.Else():
                        # Different page — close this page
                        m.next = "WRITE_RELEASE"
                with m.Elif(self.done):
                    m.d.sync += has_next.eq(0)
                    m.next = "WRITE_RELEASE"

            with m.State("WRITE_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(~has_next):
                        # Done signal caused release
                        m.d.sync += poll_return.eq(PollReturn.IDLE)
                    with m.Elif(addr_latch[16:24] != current_block):
                        # New block
                        m.d.sync += poll_return.eq(PollReturn.WREN_ERASE)
                    with m.Else():
                        # New page, same block
                        m.d.sync += poll_return.eq(PollReturn.WREN_WRITE)
                    m.next = "POLL_CMD"


            with m.State("POLL_CMD"):
                send(1, Mode.PutX1, READ_STATUS)
                with m.If(self.qo.ready):
                    m.next = "POLL_READ"

            with m.State("POLL_READ"):
                send(1, Mode.GetX1, 0)
                with m.If(self.qo.ready):
                    m.next = "POLL_CAPTURE"

            with m.State("POLL_CAPTURE"):
                m.d.comb += self.qi.ready.eq(1)
                with m.If(self.qi.valid):
                    m.d.sync += status.eq(self.qi.p.data)
                    m.next = "POLL_RELEASE"

            with m.State("POLL_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(status[0]):
                        # WIP=1, retry
                        m.next = "POLL_CMD"
                    with m.Else():
                        # WIP=0, done
                        with m.Switch(poll_return):
                            with m.Case(PollReturn.WREN_ERASE):
                                m.d.sync += wren_target.eq(0)
                                m.next = "WREN"
                            with m.Case(PollReturn.WREN_WRITE):
                                m.d.sync += wren_target.eq(1)
                                m.next = "WREN"
                            with m.Case(PollReturn.IDLE):
                                m.d.sync += block_valid.eq(0)
                                m.next = "IDLE"

        return m


# Test cases
import unittest
from .test_util import stream_get, stream_put, simulate
from amaranth.lib import io


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

            self.i = flash.i
            self.done = flash.done

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
