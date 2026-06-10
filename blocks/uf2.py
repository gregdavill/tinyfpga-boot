from amaranth import *
from amaranth.lib import data, wiring, stream
from amaranth.lib.wiring import In, Out


UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30

_stream_layout = data.StructLayout({"data": 8, "first": 1, "last": 1})

class UF2Decoder(wiring.Component):
    def __init__(self, *, base_addr=0):
        """`base_addr` relocates every block's target address into the
        physical flash region of the slot we're flashing. The host's
        UF2 file carries addresses relative to 0 (slot-agnostic);
        """
        self.base_addr = base_addr
        super().__init__({
            "i": In(stream.Signature(_stream_layout)),
            "o": Out(stream.Signature(data.StructLayout({"addr": 24, "data": 8}))),
            
            # Pulse high to reset transfer-level state (`error`, `done`). 
            "clear":     In(1),
            
            "error":     Out(1),
            "blockNo":   Out(32),
            "numBlocks": Out(32),
            "familyID":  Out(32),
            "done":      Out(1),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        byte_count = Signal(range(512))
        accum = Signal(32)

        target_addr = Signal(32)
        payload_size = Signal(32)
        data_count = Signal(32)
        flags = Signal(32)

        with m.FSM():
            with m.State("HEADER"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):

                    # Shift in byte (little-endian: first byte is LSB)
                    m.d.sync += accum.eq(Cat(accum[8:], self.i.p.data))

                    # New block starting - clear `done` from the previous transfer
                    with m.If(byte_count == 0):
                        m.d.sync += self.done.eq(0)

                    with m.Switch(byte_count):
                        with m.Case(3):
                            word = Cat(accum[8:], self.i.p.data)
                            with m.If(word != UF2_MAGIC_START0):
                                # this sector isn't a UF2 block. Skip it
                                # silently so the SCSI WRITE still succeeds
                                m.next = "DISCARD"
                        with m.Case(7):
                            word = Cat(accum[8:], self.i.p.data)
                            with m.If(word != UF2_MAGIC_START1):
                                m.next = "DISCARD"
                        with m.Case(11):
                            m.d.sync += flags.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(15):
                            m.d.sync += target_addr.eq(
                                Cat(accum[8:], self.i.p.data) + self.base_addr)
                        with m.Case(19):
                            m.d.sync += payload_size.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(23):
                            m.d.sync += self.blockNo.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(27):
                            m.d.sync += self.numBlocks.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(31):
                            # familyID, when flags bit 0x2000 is set; pick the RAM bank.
                            m.d.sync += self.familyID.eq(
                                Cat(accum[8:], self.i.p.data))
                            # All header fields latched; check flags
                            with m.If(flags & 0x0001):
                                # "not main flash" flag set → skip the block.
                                m.next = "DISCARD"
                            with m.Else():
                                m.d.sync += data_count.eq(0)
                                m.next = "DATA"

                    m.d.sync += byte_count.eq(byte_count + 1)

            with m.State("DATA"):
                with m.If(data_count == payload_size):
                    # No more payload bytes; consume padding
                    m.next = "PAD"
                with m.Else():
                    m.d.comb += [
                        self.o.valid.eq(self.i.valid),
                        self.o.p.addr.eq(target_addr[:24]),
                        self.o.p.data.eq(self.i.p.data),
                        self.i.ready.eq(self.o.ready),
                    ]
                    with m.If(self.i.valid & self.o.ready):
                        m.d.sync += target_addr.eq(target_addr + 1)
                        m.d.sync += data_count.eq(data_count + 1)
                        m.d.sync += byte_count.eq(byte_count + 1)

            with m.State("PAD"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += byte_count.eq(byte_count + 1)
                    with m.If(byte_count == 507):
                        m.d.sync += accum.eq(Cat(accum[8:], self.i.p.data))
                        m.next = "FINAL"

            with m.State("FINAL"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += accum.eq(Cat(accum[8:], self.i.p.data))
                    m.d.sync += byte_count.eq(byte_count + 1)
                    with m.If(byte_count == 511):
                        word = Cat(accum[8:], self.i.p.data)
                        with m.If(word != UF2_MAGIC_END):
                            m.d.sync += self.error.eq(1)
                        with m.Elif(self.blockNo == (self.numBlocks - 1)):
                            m.d.sync += self.done.eq(1)
                        m.d.sync += byte_count.eq(0)
                        m.d.sync += accum.eq(0)
                        m.next = "HEADER"

            with m.State("DISCARD"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += byte_count.eq(byte_count + 1)
                    with m.If(byte_count == 511):
                        m.d.sync += byte_count.eq(0)
                        m.d.sync += accum.eq(0)
                        # Don't clear `error` here: a *prior* block's bad end
                        # magic may have set it and it must persist until the
                        # transfer-level `clear`. A non-UF2 sector reaching
                        # DISCARD is itself a clean skip (it set no error).
                        m.next = "HEADER"

        # Transfer-level reset from upstream (SCSI).
        with m.If(self.clear):
            m.d.sync += self.done.eq(0)
            m.d.sync += self.error.eq(0)

        return m


# Test cases
import struct
import unittest
from .test_util import stream_get, stream_put, simulate


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
