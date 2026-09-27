import unittest
from amaranth import Elaboratable
from tests.unit.util import simulate
from gateware.staysource.no_valid_app import NoValidAppStaySource


class TestNoValidAppStaySource(unittest.TestCase):
    """Drive the QSPI response stream directly with byte sequences, modelling a
    minimal controller (accept every command, feed read bytes on demand)."""

    SYNC = 0x7EAA997E

    def _verdict(self, read_bytes, poison_word=None):
        dut = NoValidAppStaySource(app_offset=0x40000, sync_word=self.SYNC,
                                   poison_word=poison_word,
                                   probe_len=len(read_bytes))
        out = {}

        async def tb(ctx):
            ctx.set(dut.o.ready, 1)     # accept opcode/addr/dummy/get/release
            ctx.set(dut.req, 1)
            idx = 0
            for _ in range(4000):
                byte = read_bytes[idx] if idx < len(read_bytes) else 0xFF
                ctx.set(dut.i.p.data, byte)
                ctx.set(dut.i.valid, 1)
                await ctx.tick()
                if ctx.get(dut.i.ready):     # a read byte was consumed
                    idx += 1
                if ctx.get(dut.done):
                    out["stay"] = ctx.get(dut.stay)
                    return

        simulate(dut, tb)
        self.assertIn("stay", out, "reader never asserted done")
        return out["stay"]

    def test_sync_present_boots(self):
        # iCE40 sync word embedded -> looks like a bitstream -> allow auto-boot.
        seq = [0xFF, 0x00, 0x00, 0xFF, 0x7E, 0xAA, 0x99, 0x7E, 0x11, 0x22]
        self.assertEqual(self._verdict(seq), 0)

    def test_erased_stays(self):
        # All 0xFF -> erased -> keep the bootloader.
        self.assertEqual(self._verdict([0xFF] * 12), 1)

    def test_garbage_without_sync_stays(self):
        # Non-erased but no sync word -> not a (this-family) bitstream -> stay.
        self.assertEqual(self._verdict([0xDE, 0xAD, 0xBE, 0xEF] * 3), 1)

    def test_poison_overrides_sync(self):
        # Header-like layout: leading bytes, poison word, then a valid sync. With
        # poison checking enabled the slot is treated as no-valid-app -> stay.
        seq = [0xFF, 0x00, 0x5A, 0xFE, 0xB0, 0x07, 0xFF, 0x00, 0x7E, 0xAA, 0x99, 0x7E]
        self.assertEqual(self._verdict(seq, poison_word=0x5AFEB007), 1)

    def test_poison_ignored_when_disabled(self):
        # Same bytes, but poison checking off -> the sync word -> auto-boot.
        seq = [0xFF, 0x00, 0x5A, 0xFE, 0xB0, 0x07, 0xFF, 0x00, 0x7E, 0xAA, 0x99, 0x7E]
        self.assertEqual(self._verdict(seq), 0)


if __name__ == "__main__":
    unittest.main()
