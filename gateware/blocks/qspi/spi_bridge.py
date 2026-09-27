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

from .controller import Mode


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
