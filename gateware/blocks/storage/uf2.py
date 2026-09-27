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
