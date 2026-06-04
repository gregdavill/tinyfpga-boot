"""Formal spec for `HexNibbleEncoder`.

Property of interest: the encoder never emits a byte that isn't the ASCII hex
digit of one of the two nibbles of the byte currently on its input.
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.hex_encoder import HexNibbleEncoder, HEX


class HexEncoderSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = HexNibbleEncoder()

        # Free environment: arbitrary upstream payload/valid and downstream ready.
        m.d.comb += [
            dut.i.valid.eq(AnySeq(1)),
            dut.i.p.data.eq(AnySeq(8)),
            dut.o.ready.eq(AnySeq(1)),
        ]

        hexmap = Array(Const(c, 8) for c in HEX)
        hi = hexmap[dut.i.p.data[4:8]]
        lo = hexmap[dut.i.p.data[0:4]]

        with m.If(dut.o.valid):
            # Output is always the hex digit of the high or low nibble of the
            # byte presented this cycle (hence always a valid ASCII hex char).
            m.d.comb += Assert((dut.o.p.data == hi) | (dut.o.p.data == lo))

        # Both stream beats are reachable: a high-nibble emit (input held), and
        # a completed byte (input consumed -> i.ready) so the FSM cycles back.
        m.d.comb += Cover(dut.o.valid & dut.o.ready & ~dut.i.ready)
        m.d.comb += Cover(dut.o.valid & dut.o.ready & dut.i.ready)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(HexEncoderSpec(), name="hex_encoder", depth=12)
