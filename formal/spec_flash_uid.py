"""Formal spec for `FlashUID`.

The reader forwards the flash's Unique-ID bytes straight from the QSPI response
stream to its output.
It must never emit a byte that isn't a flash byte, nor emit one without a corresponding input byte. 
Sized down (2 id + 1 dummy byte) so the covers reach with shallow depth
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.flash_uid import FlashUID


class FlashUidSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = FlashUID(uid_bytes=2, dummy_bytes=1)

        # Free environment: request line + both ends of the QSPI handshake and
        # the downstream sink.
        m.d.comb += [
            dut.req.eq(AnySeq(1)),
            dut.o.ready.eq(AnySeq(1)),
            dut.i.valid.eq(AnySeq(1)),
            dut.i.p.data.eq(AnySeq(8)),
            dut.data.ready.eq(AnySeq(1)),
        ]

        # An output UID byte only ever appears alongside a QSPI response byte...
        m.d.comb += Assert(~dut.data.valid | dut.i.valid)
        # ...and is exactly that byte (forwarded, never fabricated/corrupted).
        m.d.comb += Assert(~dut.data.valid | (dut.data.p.data == dut.i.p.data))

        # A UID byte is forwarded, and the whole read transaction completes.
        m.d.comb += Cover(dut.data.valid & dut.data.ready)
        m.d.comb += Cover(dut.done)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(FlashUidSpec(), name="flash_uid", depth=14)
