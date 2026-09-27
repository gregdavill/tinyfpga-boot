import unittest
from tests.unit.util import stream_put, stream_get, simulate
from gateware.blocks.serial.json_key_parser import JsonStringKeyParser


class TestJsonStringKeyParser(unittest.TestCase):
    def _run(self, doc, key=b"uuid"):
        dut = JsonStringKeyParser(key=key)
        out = bytearray()
        result = {}

        async def consume(ctx):
            ctx.set(dut.o.ready, 1)
            for _ in range(500):
                if ctx.get(dut.done):
                    break
                if ctx.get(dut.o.valid):
                    out.append(ctx.get(dut.o.p.data))
                await ctx.tick()

        async def drive(ctx):
            for b in doc:
                if ctx.get(dut.done):
                    break
                await stream_put(ctx, dut.i, {'data': b})
            for _ in range(20):
                if ctx.get(dut.done):
                    break
                await ctx.tick()
            result['done'] = ctx.get(dut.done)

        simulate(dut, consume, drive)
        result['value'] = bytes(out)
        return result

    def test_extracts_value(self):
        doc = b'{"boardmeta": {"name": "TinyFPGA BX", "uuid": "1234-abcd"}}'
        r = self._run(doc)
        self.assertTrue(r['done'])
        self.assertEqual(r['value'], b'1234-abcd')

    def test_ignores_substring_in_other_value(self):
        doc = b'{"note": "uuid lives here", "uuid": "real"}'
        r = self._run(doc)
        self.assertTrue(r['done'])
        self.assertEqual(r['value'], b'real')

    def test_missing_key_never_done(self):
        doc = b'{"name": "TinyFPGA BX"}'
        r = self._run(doc)
        self.assertFalse(r['done'])
        self.assertEqual(r['value'], b'')

    def test_empty_value(self):
        doc = b'{"uuid": ""}'
        r = self._run(doc)
        self.assertTrue(r['done'])
        self.assertEqual(r['value'], b'')


if __name__ == "__main__":
    unittest.main()
