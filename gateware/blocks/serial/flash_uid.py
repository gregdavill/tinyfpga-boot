from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data
from gateware.blocks.qspi.controller import Mode


UID_CMD = 0x4B

class FlashUID(wiring.Component):
    """Read the flash's Unique ID and stream it out byte by byte.

    Issues the Read Unique ID command (0x4B) over QSPI and forwards the
    `uid_bytes` ID bytes (after the leading `dummy_bytes`) on a byte-wide
    output stream, in the order they come off the wire. `done` pulses once
    the read transaction finishes.
    """

    def __init__(self, uid_bytes = 8, dummy_bytes = 4):
        self._uid_bytes = uid_bytes
        self._dummy_bytes = dummy_bytes
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

            # Request
            'req': In(1),

            # Read transaction finished.
            'done': Out(1),
            # UID byte stream
            'data': Out(stream.Signature(data.StructLayout({
                "data": 8
            }))),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        total = self._uid_bytes + self._dummy_bytes
        write_count = Signal(range(1 + total), init=total - 1)
        read_count = Signal(range(1 + total), init=total - 1)

        # Write Path (OP + DUMMY + UUID)
        with m.FSM():
            with m.State('IDLE'):
                m.d.comb += [
                    # Send UUID command to QSPI
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.PutX1),
                    self.o.p.data.eq(UID_CMD),
                    self.o.valid.eq(self.req),
                ]
                with m.If(self.req & self.o.ready):
                    m.next = 'READ'

            with m.State('READ'):
                m.d.comb += [
                    # Request dummy + UUID bytes from SPI
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.GetX1),
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.d.sync += write_count.eq(write_count - 1)
                    with m.If(write_count == 0):
                        m.next = 'RELEASE'

            with m.State('RELEASE'):
                m.d.comb += [
                    # Release CS line
                    self.o.p.chip.eq(0),
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.next = 'DONE'

            with m.State('DONE'):
                ...

        # Read Path (drop the dummy bytes, stream out the UID bytes)
        with m.FSM() as fsm:
            m.d.comb += self.done.eq(fsm.ongoing('DONE'))

            with m.State('READ'):
                # The last `uid_bytes` of the transfer are the ID; the
                # leading `dummy_bytes` are clocked out and discarded.
                with m.If(read_count < self._uid_bytes):
                    m.d.comb += [
                        self.data.p.data.eq(self.i.p.data),
                        self.data.valid.eq(self.i.valid),
                        self.i.ready.eq(self.data.ready),
                    ]
                with m.Else():
                    m.d.comb += self.i.ready.eq(1)      # discard dummy byte
                with m.If(self.i.valid & self.i.ready):
                    m.d.sync += read_count.eq(read_count - 1)
                    with m.If(read_count == 0):
                        m.next = 'DONE'

            with m.State('DONE'):
                ...

        return m
