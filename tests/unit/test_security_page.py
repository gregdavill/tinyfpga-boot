import unittest
from tests.unit.util import stream_put, stream_get, simulate
from gateware.blocks.serial.security_page import SecurityPage


class TestSecurityPage(unittest.TestCase):
    def test_streams_page_bytes(self):
        """Bytes arriving on `i` (the QSPI read path) are forwarded on
        `data`, and `done` asserts once `abort` ends the read."""
        page = b'{"uuid": "abc"}'
        dut = SecurityPage(read_len=255)
        got = bytearray()
        result = {}

        async def consume(ctx):
            for _ in range(len(page)):
                got.append((await stream_get(ctx, dut.data))['data'])

        async def drive(ctx):
            ctx.set(dut.o.ready, 1)     # accept every QSPI command token
            ctx.set(dut.req, 1)
            for b in page:
                await stream_put(ctx, dut.i, {'data': b})
            ctx.set(dut.abort, 1)       # stop the (otherwise 255-byte) read
            for _ in range(50):
                if ctx.get(dut.done):
                    break
                await ctx.tick()
            result['done'] = ctx.get(dut.done)

        simulate(dut, consume, drive)
        self.assertEqual(bytes(got), page)
        self.assertTrue(result['done'])


if __name__ == "__main__":
    unittest.main()
