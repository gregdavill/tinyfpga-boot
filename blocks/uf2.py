from amaranth import *
from amaranth.lib import data, wiring, stream
from amaranth.lib.wiring import In, Out


UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30


class UF2Decoder(wiring.Component):
    def __init__(self):
        super().__init__({
            "i": In(stream.Signature(data.StructLayout({"data": 8}))),
            "o": Out(stream.Signature(data.StructLayout({"addr": 24, "data": 8}))),
            "error":     Out(1),
            "blockNo":   Out(32),
            "numBlocks": Out(32),
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

                    with m.Switch(byte_count):
                        with m.Case(3):
                            word = Cat(accum[8:], self.i.p.data)
                            with m.If(word != UF2_MAGIC_START0):
                                m.d.sync += self.error.eq(1)
                                m.next = "DISCARD"
                        with m.Case(7):
                            word = Cat(accum[8:], self.i.p.data)
                            with m.If(word != UF2_MAGIC_START1):
                                m.d.sync += self.error.eq(1)
                                m.next = "DISCARD"
                        with m.Case(11):
                            m.d.sync += flags.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(15):
                            m.d.sync += target_addr.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(19):
                            m.d.sync += payload_size.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(23):
                            m.d.sync += self.blockNo.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(27):
                            m.d.sync += self.numBlocks.eq(Cat(accum[8:], self.i.p.data))
                        with m.Case(31):
                            # All header fields latched; check flags
                            with m.If(flags & 0x0001):
                                # "not main flash" flag set → skip block
                                m.d.sync += self.error.eq(1)
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
                        m.d.sync += self.error.eq(0)
                        m.next = "HEADER"

        return m


# Test cases
import struct
import unittest
from amaranth.sim import Simulator


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


async def stream_get(ctx, stream):
    ctx.set(stream.ready, 1)
    payload, = await ctx.tick().sample(stream.payload).until(stream.valid)
    ctx.set(stream.ready, 0)
    return payload


async def stream_put(ctx, stream, payload):
    ctx.set(stream.valid, 1)
    ctx.set(stream.payload, payload)
    await ctx.tick().until(stream.ready)
    ctx.set(stream.valid, 0)


class TestUF2Decoder(unittest.TestCase):

    def test_valid_block(self):
        """Feed a valid UF2 block and check output addr/data pairs."""
        dut = UF2Decoder()
        payload = bytes(range(16))
        block = make_uf2_block(addr=0x1000, data_bytes=payload, block_no=0, num_blocks=1)

        received = []

        async def feeder(ctx):
            for b in block:
                await stream_put(ctx, dut.i, {"data": b})
            # Let done propagate
            await ctx.tick().repeat(3)

        async def checker(ctx):
            for _ in range(len(payload)):
                p = await stream_get(ctx, dut.o)
                received.append((p["addr"], p["data"]))

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(feeder)
        sim.add_testbench(checker)
        with sim.write_vcd("uf2_valid.vcd"):
            sim.run()

        for i, (addr, d) in enumerate(received):
            self.assertEqual(addr, 0x1000 + i, f"byte {i}: addr mismatch")
            self.assertEqual(d, payload[i], f"byte {i}: data mismatch")
        self.assertEqual(len(received), len(payload))

    def test_bad_magic_start(self):
        """A block with corrupted magicStart0 should assert error and produce no output."""
        dut = UF2Decoder()
        payload = bytes(range(8))
        block = bytearray(make_uf2_block(addr=0x2000, data_bytes=payload))
        # Corrupt first magic word
        block[0] = 0xFF

        output_count = 0
        error_seen = False

        async def feeder(ctx):
            for b in block:
                await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(3)

        async def monitor(ctx):
            nonlocal output_count, error_seen
            for _ in range(600):
                await ctx.tick()
                if ctx.get(dut.o.valid):
                    output_count += 1
                if ctx.get(dut.error):
                    error_seen = True

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(feeder)
        sim.add_testbench(monitor)
        with sim.write_vcd("uf2_bad_magic.vcd"):
            sim.run()

        self.assertTrue(error_seen, "error should be asserted on bad magic")
        self.assertEqual(output_count, 0, "no output should be produced for bad block")

    def test_not_main_flash_flag(self):
        """A block with flags bit 0 set should be skipped (no output, error asserted)."""
        dut = UF2Decoder()
        payload = bytes(range(8))
        block = make_uf2_block(addr=0x3000, data_bytes=payload, flags=0x0001)

        output_count = 0
        error_seen = False

        async def feeder(ctx):
            for b in block:
                await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(3)

        async def monitor(ctx):
            nonlocal output_count, error_seen
            for _ in range(600):
                await ctx.tick()
                if ctx.get(dut.o.valid):
                    output_count += 1
                if ctx.get(dut.error):
                    error_seen = True

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(feeder)
        sim.add_testbench(monitor)
        with sim.write_vcd("uf2_skip_flag.vcd"):
            sim.run()

        self.assertTrue(error_seen, "error should be asserted when not-main-flash flag is set")
        self.assertEqual(output_count, 0, "no output for skipped block")

    def test_done_signal(self):
        """done should assert when the final block of a multi-block transfer completes."""
        dut = UF2Decoder()
        payload = bytes([0xAA] * 4)
        blocks = []
        for i in range(3):
            blocks.append(make_uf2_block(
                addr=0x1000 + i * 4, data_bytes=payload,
                block_no=i, num_blocks=3
            ))

        done_seen = False

        async def feeder(ctx):
            for block in blocks:
                for b in block:
                    await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(5)

        async def output_sink(ctx):
            """Consume output to prevent backpressure stalls."""
            nonlocal done_seen
            for _ in range(2000):
                await ctx.tick()
                ctx.set(dut.o.ready, 1)
                if ctx.get(dut.done):
                    done_seen = True

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(feeder)
        sim.add_testbench(output_sink)
        with sim.write_vcd("uf2_done.vcd"):
            sim.run()

        self.assertTrue(done_seen, "done should assert after final block")

    def test_bad_magic_end(self):
        """A block with corrupted final magic should assert error."""
        dut = UF2Decoder()
        payload = bytes(range(4))
        block = bytearray(make_uf2_block(addr=0x4000, data_bytes=payload))
        # Corrupt final magic (last 4 bytes)
        block[508] = 0xFF

        error_seen = False

        async def feeder(ctx):
            for b in block:
                await stream_put(ctx, dut.i, {"data": b})
            await ctx.tick().repeat(3)

        async def output_sink(ctx):
            nonlocal error_seen
            for _ in range(600):
                await ctx.tick()
                ctx.set(dut.o.ready, 1)
                if ctx.get(dut.error):
                    error_seen = True

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(feeder)
        sim.add_testbench(output_sink)
        with sim.write_vcd("uf2_bad_end.vcd"):
            sim.run()

        self.assertTrue(error_seen, "error should be asserted on bad final magic")


if __name__ == "__main__":
    unittest.main()
