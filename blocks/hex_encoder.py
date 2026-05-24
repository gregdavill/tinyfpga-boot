"""Expand a byte stream into its lowercase ASCII hex representation.

Each input byte becomes two output bytes: the high nibble's hex digit
then the low nibble's. So `0xCA` → `b"ca"`.
"""

from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data


HEX = b'0123456789abcdef'


class HexNibbleEncoder(wiring.Component):
    i: In(stream.Signature(data.StructLayout({"data": 8})))
    o: Out(stream.Signature(data.StructLayout({"data": 8})))

    def elaborate(self, platform) -> Module:
        m = Module()

        hexmap = Array(Const(c, 8) for c in HEX)
        byte = self.i.p.data

        with m.FSM():
            # Most-significant nibble first ("ca" for 0xCA). The input byte
            # is held (i.ready stays low) until both nibbles are emitted.
            with m.State('HIGH'):
                m.d.comb += [
                    self.o.valid.eq(self.i.valid),
                    self.o.p.data.eq(hexmap[byte[4:8]]),
                ]
                with m.If(self.i.valid & self.o.ready):
                    m.next = 'LOW'

            with m.State('LOW'):
                m.d.comb += [
                    self.o.valid.eq(self.i.valid),
                    self.o.p.data.eq(hexmap[byte[0:4]]),
                    self.i.ready.eq(self.o.ready),     # consume after low nibble
                ]
                with m.If(self.i.valid & self.o.ready):
                    m.next = 'HIGH'

        return m


# Test cases
import unittest
from .test_util import stream_put, stream_get, simulate


class TestHexNibbleEncoder(unittest.TestCase):
    def _run(self, payload: bytes) -> bytes:
        dut = HexNibbleEncoder()
        out = bytearray()

        async def feed(ctx):
            for b in payload:
                await stream_put(ctx, dut.i, {'data': b})

        async def consume(ctx):
            ctx.set(dut.o.ready, 1)
            for _ in range(20 * (len(payload) + 1)):
                if ctx.get(dut.o.valid):
                    out.append(ctx.get(dut.o.p.data))
                await ctx.tick()

        simulate(dut, feed, consume)
        return bytes(out)

    def test_encodes_bytes(self):
        self.assertEqual(self._run(b"\xCA\xFE\xBA\xBE"), b"cafebabe")

    def test_low_and_high_nibbles(self):
        self.assertEqual(self._run(b"\x05\xA0\x0F"), b"05a00f")


if __name__ == "__main__":
    unittest.main()
