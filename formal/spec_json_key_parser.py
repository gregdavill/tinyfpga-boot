"""Formal spec for `JsonStringKeyParser`.

The parser re-emits a key's string value byte for byte.
 * every emitted byte is an input byte, only appears alongside a valid input
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.json_key_parser import JsonStringKeyParser


class JsonKeyParserSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = JsonStringKeyParser(key=b"uuid")

        m.d.comb += [
            dut.i.valid.eq(AnySeq(1)),
            dut.i.p.data.eq(AnySeq(8)),
            dut.o.ready.eq(AnySeq(1)),
        ]

        # A value byte only appears alongside a valid input byte...
        m.d.comb += Assert(~dut.o.valid | dut.i.valid)
        # ...is exactly that input byte (forwarded verbatim)...
        m.d.comb += Assert(~dut.o.valid | (dut.o.p.data == dut.i.p.data))
        # ...and is never the closing quote that terminates the value.
        m.d.comb += Assert(~dut.o.valid | (dut.o.p.data != ord('"')))

        # A value byte can be emitted, and the key/value can be fully matched.
        m.d.comb += Cover(dut.o.valid & dut.o.ready)
        m.d.comb += Cover(dut.done)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(JsonKeyParserSpec(), name="json_key_parser", depth=16)
