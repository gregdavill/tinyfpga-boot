"""Extract a JSON string value from a byte stream.

Scans an incoming byte stream for a fixed JSON string key (e.g.
``"uuid"``) and re-emits that key's string value on an output byte
stream. Only the strict JSON-string form is recognised:

    "<key>"  :  "<value>"

Closing quote ends the capture and pulses ``done``. 
Escapes are not interpreted

If the key never appears, ``done`` never asserts and the 
upstream reader terminates on its own length.
"""

from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data


class JsonStringKeyParser(wiring.Component):
    def __init__(self, key=b"uuid"):
        # Match the quoted key so a bare substring elsewhere in the
        # document can't trip the search.
        self._key = b'"' + key + b'"'
        super().__init__({
            'i': In(stream.Signature(data.StructLayout({"data": 8}))),
            'o': Out(stream.Signature(data.StructLayout({"data": 8}))),
            'done': Out(1),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        key = Array(Const(b, 8) for b in self._key)
        match_pos = Signal(range(len(self._key) + 1))
        rx = self.i.p.data

        with m.FSM() as fsm:
            m.d.comb += self.done.eq(fsm.ongoing('DONE'))

            with m.State('SEARCH'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    with m.If(rx == key[match_pos]):
                        with m.If(match_pos == len(self._key) - 1):
                            m.d.sync += match_pos.eq(0)
                            m.next = 'SEEK_COLON'
                        with m.Else():
                            m.d.sync += match_pos.eq(match_pos + 1)
                    with m.Else():
                        # Allow an immediate restart if this byte is the
                        # key's first char (the opening quote).
                        m.d.sync += match_pos.eq(Mux(rx == key[0], 1, 0))

            with m.State('SEEK_COLON'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid & (rx == ord(':'))):
                    m.next = 'SEEK_VALUE'

            with m.State('SEEK_VALUE'):
                m.d.comb += self.i.ready.eq(1)
                # Skip whitespace; the opening quote starts the value.
                with m.If(self.i.valid & (rx == ord('"'))):
                    m.next = 'CAPTURE'

            with m.State('CAPTURE'):
                with m.If(self.i.valid):
                    with m.If(rx == ord('"')):
                        m.d.comb += self.i.ready.eq(1)   # consume the quote
                        m.next = 'DONE'
                    with m.Else():
                        # Forward the value byte; only consume when the
                        # downstream sink accepts it.
                        m.d.comb += [
                            self.o.valid.eq(1),
                            self.o.p.data.eq(rx),
                            self.i.ready.eq(self.o.ready),
                        ]

            with m.State('DONE'):
                # Drain (and discard) any trailing input so the upstream
                # reader is never back-pressured while it winds down.
                m.d.comb += self.i.ready.eq(1)

        return m
