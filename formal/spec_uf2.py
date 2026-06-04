"""Formal spec for `UF2Decoder`.

The decoder unpacks 512-byte UF2 blocks and streams out (addr, data) pairs. 
The properties pin down the payload path: 
 * a written data byte only appears alongside a valid input byte and equals it verbatim
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.uf2 import UF2Decoder


class Uf2Spec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = UF2Decoder()

        m.d.comb += [
            dut.i.valid.eq(AnySeq(1)),
            dut.i.p.data.eq(AnySeq(8)),
            dut.i.p.first.eq(AnySeq(1)),
            dut.i.p.last.eq(AnySeq(1)),
            dut.o.ready.eq(AnySeq(1)),
            dut.clear.eq(AnySeq(1)),
        ]

        # A payload byte only emerges alongside a valid input byte...
        m.d.comb += Assert(~dut.o.valid | dut.i.valid)
        # ...and is exactly that byte (data is passed through; only the address
        # is relocated).
        m.d.comb += Assert(~dut.o.valid | (dut.o.p.data == dut.i.p.data))

        # A corrupt header is flagged, and (deeper) a payload byte streams out.
        m.d.comb += Cover(dut.error)
        m.d.comb += Cover(dut.o.valid & dut.o.ready)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(Uf2Spec(), name="uf2", depth=40)
