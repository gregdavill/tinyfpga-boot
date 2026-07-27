"""Formal spec for `QspiFlash`.

The page-program sequencer drives a flash over a single QSPI command stream (`qo`). 

Two invariants hold across its whole FSM, regardless of input:
  * every chip-select release (chip 0) is a `Dummy` token
  * every chip-selected command (chip 1) is currently single-lane (`PutX1` / `GetX1`)
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.flash import QspiFlash
from blocks.qspi import Mode


class FlashSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = QspiFlash()

        m.d.comb += [
            dut.port.w.valid.eq(AnySeq(1)),
            dut.port.w.p.addr.eq(AnySeq(24)),
            dut.port.w.p.data.eq(AnySeq(8)),
            dut.qo.ready.eq(AnySeq(1)),
            dut.qi.valid.eq(AnySeq(1)),
            dut.qi.p.data.eq(AnySeq(8)),
            dut.port.flush.eq(AnySeq(1)),
        ]

        chip = dut.qo.p.chip
        mode = dut.qo.p.mode
        with m.If(dut.qo.valid):
            # Releasing CS (chip 0) is always a Dummy token.
            m.d.comb += Assert(~(chip == 0) | (mode == Mode.Dummy))
            # A selected command (chip 1) is always single-lane.
            m.d.comb += Assert((chip == 0)
                               | (mode == Mode.PutX1) | (mode == Mode.GetX1))

        # Both token kinds are reachable: a CS release and a status-poll read.
        m.d.comb += Cover(dut.qo.valid & (chip == 0))
        m.d.comb += Cover(dut.qo.valid & (mode == Mode.GetX1))

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(FlashSpec(), name="flash", depth=24)
