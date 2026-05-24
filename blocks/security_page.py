"""Read the flash security page and stream its bytes out.

The security page is read with opcode ``0x48``

The transaction self-terminates after ``read_len`` bytes, or earlier if
``abort`` is asserted.

``done`` pulses high once CS# is released.
"""

from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data

from .qspi import Mode


SECURITY_READ_CMD = 0x48           # Adesto/Renesas Read Security Registers


class SecurityPage(wiring.Component):
    def __init__(self, *, page=1, read_len=255, opcode=SECURITY_READ_CMD,
                 addr_offset_bits=0):
        self._read_len = read_len

        addr = page << (8 + addr_offset_bits)
        # Prologue clocked out MSB-first via PutX1: opcode, 3 address
        # bytes, 1 dummy byte.
        self._prologue = [
            opcode,
            (addr >> 16) & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            0x00,
        ]

        super().__init__({
            # FLASH QSPI interface
            'o': Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8
            }))),
            'i': In(stream.Signature(data.StructLayout({
                "data": 8
            }))),

            # Control
            'req': In(1),
            'abort': In(1),
            'done': Out(1),

            # Page contents, streamed out one byte at a time.
            'data': Out(stream.Signature(data.StructLayout({
                "data": 8
            }))),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        prologue = Array(Const(b, 8) for b in self._prologue)

        # Forward QSPI read bytes straight onto the output stream. 
        # Only GetX1 frames produce octets here, PutX1 prologue / CS-release
        # tokens never leak through.
        m.d.comb += [
            self.data.p.data.eq(self.i.p.data),
            self.data.valid.eq(self.i.valid),
            self.i.ready.eq(self.data.ready),
        ]

        send_idx = Signal(range(len(self._prologue)))
        read_count = Signal(range(self._read_len + 1), init=self._read_len - 1)

        with m.FSM() as fsm:
            m.d.comb += self.done.eq(fsm.ongoing('DONE'))

            with m.State('PROLOGUE'):
                m.d.comb += [
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.PutX1),
                    self.o.p.data.eq(prologue[send_idx]),
                    self.o.valid.eq(self.req),
                ]
                with m.If(self.req & self.o.ready):
                    with m.If(send_idx == len(self._prologue) - 1):
                        m.next = 'READ'
                    with m.Else():
                        m.d.sync += send_idx.eq(send_idx + 1)

            with m.State('READ'):
                m.d.comb += [
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.GetX1),
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.d.sync += read_count.eq(read_count - 1)
                    with m.If((read_count == 0) | self.abort):
                        m.next = 'RELEASE'

            with m.State('RELEASE'):
                m.d.comb += [
                    self.o.p.chip.eq(0),     # deassert CS#
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.next = 'DONE'

            with m.State('DONE'):
                ...

        return m


# Test cases
import unittest
from .test_util import stream_put, stream_get, simulate


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
