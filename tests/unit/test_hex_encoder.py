import unittest
from tests.unit.util import stream_put, stream_get, simulate
from gateware.blocks.serial.hex_encoder import HexNibbleEncoder


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
