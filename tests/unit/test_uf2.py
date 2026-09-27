import struct
import unittest
from tests.unit.util import stream_get, stream_put, simulate
from gateware.blocks.storage.uf2 import UF2Decoder, UF2_MAGIC_END, UF2_MAGIC_START0, UF2_MAGIC_START1


def make_uf2_block(addr, data_bytes, block_no=0, num_blocks=1, flags=0, family_id=0):
    """Construct a 512-byte UF2 block."""
    payload_size = len(data_bytes)
    assert payload_size <= 476
    header = struct.pack("<IIIIIIII",
        UF2_MAGIC_START0,
        UF2_MAGIC_START1,
        flags,
        addr,
        payload_size,
        block_no,
        num_blocks,
        family_id,
    )
    assert len(header) == 32
    payload_padded = data_bytes + bytes(476 - payload_size)
    final_magic = struct.pack("<I", UF2_MAGIC_END)
    block = header + payload_padded + final_magic
    assert len(block) == 512
    return block


class TestUF2Decoder(unittest.TestCase):

    def setUp(self):
        self.dut = UF2Decoder()
        self.error_seen = False
        self.output_count = 0
        self.done_seen = False
        self.received = []

    def feed_blocks(self, *blocks):
        dut = self.dut
        async def feeder(ctx):
            for block in blocks:
                for b in block:
                    await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(5)
        return feeder

    def monitor(self, cycles=600):
        dut = self.dut
        async def _monitor(ctx):
            for _ in range(cycles):
                await ctx.tick()
                ctx.set(dut.o.ready, 1)
                if ctx.get(dut.o.valid):
                    self.output_count += 1
                if ctx.get(dut.error):
                    self.error_seen = True
                if ctx.get(dut.done):
                    self.done_seen = True
        return _monitor

    def test_valid_block(self):
        """Feed a valid UF2 block and check output addr/data pairs."""
        payload = bytes(range(16))
        block = make_uf2_block(addr=0x1000, data_bytes=payload, block_no=0, num_blocks=1)
        dut = self.dut

        async def checker(ctx):
            for _ in range(len(payload)):
                p = await stream_get(ctx, dut.o)
                self.received.append((p["addr"], p["data"]))

        simulate(self.dut, self.feed_blocks(block), checker)

        for i, (addr, d) in enumerate(self.received):
            self.assertEqual(addr, 0x1000 + i, f"byte {i}: addr mismatch")
            self.assertEqual(d, payload[i], f"byte {i}: data mismatch")
        self.assertEqual(len(self.received), len(payload))

    def test_base_addr_relocation(self):
        """With a non-zero `base_addr`, output addresses are the UF2
        block's target address plus the base
        """
        base = 0x28000
        payload = bytes(range(16))
        block = make_uf2_block(addr=0x1000, data_bytes=payload, block_no=0, num_blocks=1)
        dut = UF2Decoder(base_addr=base)
        received = []

        async def checker(ctx):
            for _ in range(len(payload)):
                p = await stream_get(ctx, dut.o)
                received.append((p["addr"], p["data"]))

        async def feeder(ctx):
            for b in block:
                await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(5)

        simulate(dut, feeder, checker)

        for i, (addr, d) in enumerate(received):
            self.assertEqual(addr, base + 0x1000 + i, f"byte {i}: relocated addr mismatch")
            self.assertEqual(d, payload[i], f"byte {i}: data mismatch")
        self.assertEqual(len(received), len(payload))

    def test_non_uf2_sector_skipped(self):
        """A sector without UF2 start magic (e.g. a FAT/directory sector the OS
        writes when copying onto the drive) is skipped silently: no error, no
        output. Erroring here fails the SCSI WRITE and may hang the actual UF2 copy."""
        block = bytearray(make_uf2_block(addr=0x2000, data_bytes=bytes(range(8))))
        block[0] = 0xFF

        simulate(self.dut, self.feed_blocks(block), self.monitor())

        self.assertFalse(self.error_seen, "a non-UF2 sector must not raise error")
        self.assertEqual(self.output_count, 0, "no output for a skipped sector")

    def test_metadata_between_blocks(self):
        """The real drag-drop case: the OS interleaves filesystem-metadata
        sectors with the UF2 file data. Both UF2 blocks must still decode, the
        metadata sector is skipped, and no error is raised."""
        payload = bytes(range(16))
        b0 = make_uf2_block(addr=0x1000, data_bytes=payload, block_no=0, num_blocks=2)
        b1 = make_uf2_block(addr=0x1010, data_bytes=payload, block_no=1, num_blocks=2)
        meta = bytearray(512)
        meta[0:4] = b"\xDE\xAD\xBE\xEF"   # not UF2 magic

        simulate(self.dut, self.feed_blocks(b0, bytes(meta), b1),
                 self.monitor(cycles=3000))

        self.assertFalse(self.error_seen, "metadata between blocks must not error")
        self.assertEqual(self.output_count, 2 * len(payload),
                         "both blocks' payloads should decode")
        self.assertTrue(self.done_seen, "done should assert after the final block")

    def test_not_main_flash_flag(self):
        """A block with flags bit 0 set should be skipped silently:
        no output, and `error` stays low. 
        """
        block = make_uf2_block(addr=0x3000, data_bytes=bytes(range(8)), flags=0x0001)

        simulate(self.dut, self.feed_blocks(block), self.monitor())

        self.assertFalse(self.error_seen, "not-main-flash is a clean skip, not an error")
        self.assertEqual(self.output_count, 0, "no output for skipped block")

    def test_done_signal(self):
        """done should assert when the final block of a multi-block transfer completes."""
        payload = bytes([0xAA] * 4)
        blocks = [
            make_uf2_block(addr=0x1000 + i * 4, data_bytes=payload, block_no=i, num_blocks=3)
            for i in range(3)
        ]

        simulate(self.dut, self.feed_blocks(*blocks), self.monitor(cycles=2000))

        self.assertTrue(self.done_seen, "done should assert after final block")

    def test_bad_magic_end(self):
        """A block with corrupted final magic should assert error."""
        block = bytearray(make_uf2_block(addr=0x4000, data_bytes=bytes(range(4))))
        block[508] = 0xFF

        simulate(self.dut, self.feed_blocks(block), self.monitor())

        self.assertTrue(self.error_seen, "error should be asserted on bad final magic")


if __name__ == "__main__":
    unittest.main()
