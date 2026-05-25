"""TinyFPGA serial-bootloader USB<->SPI bridge.

The host (`tinyprog`) frames every flash operation as one raw SPI transaction
and a single command byte selects between booting and an SPI transfer:

    0x00                              -> pulse `boot` (arm reconfigure)
    0x01 <wlen:u16le> <rlen:u16le>
         <wlen bytes>                 -> shifted out to MOSI (CS asserted)
         <rlen bytes>                 <- clocked in from MISO, returned on `tx`
                                         CS released afterwards

The whole 0x01 transaction holds CS low: `wlen` PutX1 octets, then `rlen`
GetX1 octets, then a Dummy octet to deassert. This maps directly onto the
shared QSPI `Controller` (`chip=1` keeps CS#; `chip=0` releases it), exactly
the pattern used by `flash.py` / `serial_source.py`.

Read bytes are streamed back over `tx` with `first`/`last` so the USB IN
endpoint flushes a short packet at the end of each transaction.
"""

from amaranth import *
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out

from .qspi import Mode


_byte_stream = data.StructLayout({"data": 8, "first": 1, "last": 1})


class SpiBridge(wiring.Component):
    def __init__(self):
        super().__init__({
            # USB serial byte streams.
            "rx":  In(stream.Signature(_byte_stream)),    # from USB OUT endpoint
            "tx":  Out(stream.Signature(_byte_stream)),   # to USB IN endpoint

            # Shared QSPI controller.
            "qo":  Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "qi":  In(stream.Signature(data.StructLayout({"data": 8}))),

            "boot":     Out(1),   # 1-cycle pulse on a 0x00 command
            "activity": Out(1),   # high while a transaction is in flight
        })

    def elaborate(self, platform):
        m = Module()

        out_cnt  = Signal(16)   # write bytes remaining
        in_cnt   = Signal(16)   # read bytes remaining
        wlen_lo  = Signal(8)
        rlen_lo  = Signal(8)
        in_first = Signal()     # marks the first returned read byte

        m.d.comb += self.activity.eq(self.rx.valid | self.tx.valid | self.qo.valid)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.qo.p.chip.eq(chip),
                self.qo.p.mode.eq(mode),
                self.qo.p.data.eq(d),
                self.qo.valid.eq(1),
            ]

        with m.FSM():
            with m.State("CMD_IDLE"):
                m.d.comb += self.rx.ready.eq(1)
                with m.If(self.rx.valid):
                    with m.If(self.rx.p.data == 0x00):
                        m.d.comb += self.boot.eq(1)
                    with m.Elif(self.rx.p.data == 0x01):
                        m.next = "WLEN_LO"
                    # Any other byte is ignored (lets the framing resync).

            with m.State("WLEN_LO"):
                m.d.comb += self.rx.ready.eq(1)
                with m.If(self.rx.valid):
                    m.d.sync += wlen_lo.eq(self.rx.p.data)
                    m.next = "WLEN_HI"

            with m.State("WLEN_HI"):
                m.d.comb += self.rx.ready.eq(1)
                with m.If(self.rx.valid):
                    m.d.sync += out_cnt.eq(Cat(wlen_lo, self.rx.p.data))
                    m.next = "RLEN_LO"

            with m.State("RLEN_LO"):
                m.d.comb += self.rx.ready.eq(1)
                with m.If(self.rx.valid):
                    m.d.sync += rlen_lo.eq(self.rx.p.data)
                    m.next = "RLEN_HI"

            with m.State("RLEN_HI"):
                m.d.comb += self.rx.ready.eq(1)
                with m.If(self.rx.valid):
                    rlen = Cat(rlen_lo, self.rx.p.data)
                    m.d.sync += [
                        in_cnt.eq(rlen),
                        in_first.eq(1),
                    ]
                    with m.If(out_cnt != 0):
                        m.next = "DO_OUT"
                    with m.Elif(rlen != 0):
                        m.next = "DO_IN_REQ"
                    with m.Else():
                        m.next = "RELEASE"

            with m.State("DO_OUT"):
                # Shift one write byte to MOSI per rx/qo handshake; CS held.
                m.d.comb += [
                    self.qo.p.chip.eq(1),
                    self.qo.p.mode.eq(Mode.PutX1),
                    self.qo.p.data.eq(self.rx.p.data),
                    self.qo.valid.eq(self.rx.valid),
                    self.rx.ready.eq(self.qo.ready),
                ]
                with m.If(self.rx.valid & self.qo.ready):
                    m.d.sync += out_cnt.eq(out_cnt - 1)
                    with m.If(out_cnt == 1):
                        with m.If(in_cnt != 0):
                            m.next = "DO_IN_REQ"
                        with m.Else():
                            m.next = "RELEASE"

            with m.State("DO_IN_REQ"):
                # Clock the flash for one byte; MISO captured by the controller.
                send(1, Mode.GetX1, 0)
                with m.If(self.qo.ready):
                    m.next = "DO_IN_RESP"

            with m.State("DO_IN_RESP"):
                m.d.comb += [
                    self.tx.p.data.eq(self.qi.p.data),
                    self.tx.p.first.eq(in_first),
                    self.tx.p.last.eq(in_cnt == 1),
                    self.tx.valid.eq(self.qi.valid),
                    self.qi.ready.eq(self.tx.ready),
                ]
                with m.If(self.qi.valid & self.tx.ready):
                    m.d.sync += in_first.eq(0)
                    m.d.sync += in_cnt.eq(in_cnt - 1)
                    with m.If(in_cnt == 1):
                        m.next = "RELEASE"
                    with m.Else():
                        m.next = "DO_IN_REQ"

            with m.State("RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "CMD_IDLE"

        return m


# Test cases
import unittest
from .qspi import Controller, PortGroup
from .test_util import stream_get, stream_put, simulate
from amaranth.lib import io


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
