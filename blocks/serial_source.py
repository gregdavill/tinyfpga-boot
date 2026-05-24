"""Wrapper for device specific serial sources.

    .o / .i   QSPI command / response streams

    .req      start the boot-time read
    .done     read transaction finished
    .data     the serial, one ASCII byte at a time
    
    .max_len  upper bound on the serial length (sizes the descriptor buffer)

"""

from amaranth import *
from amaranth.lib.wiring import In, Out, connect, flipped
from amaranth.lib import wiring, stream, data

from .qspi import Mode
from .flash_uid import FlashUID
from .hex_encoder import HexNibbleEncoder
from .security_page import SecurityPage
from .json_key_parser import JsonStringKeyParser


class _SerialSource(wiring.Component):
    """Common interface; subclasses build the reader + transform chain in
    `_chain()` and return its output stream."""

    # QSPI command stream (to the controller)
    o: Out(stream.Signature(data.StructLayout({
        "chip": range(2),
        "mode": Mode,
        "data": 8
        })))

    # QSPI response stream (from the controller)
    i: In(stream.Signature(data.StructLayout({"data": 8})))  
    req: In(1)
    done: Out(1)
    # ASCII serial byte stream
    data: Out(stream.Signature(data.StructLayout({"data": 8})))  

    def elaborate(self, platform):
        m = Module()
        reader, out_stream = self._chain(m)
        # Pass the reader's QSPI port + control through, and expose the
        # transform's output as the serial byte stream.
        connect(m, flipped(self.o), reader.o)
        connect(m, reader.i, flipped(self.i))
        connect(m, flipped(self.data), out_stream)
        m.d.comb += [
            reader.req.eq(self.req),
            self.done.eq(reader.done),
        ]
        return m


class FlashUidSerialSource(_SerialSource):
    """Flash Unique ID (0x4B), rendered as a lowercase-hex ASCII stream."""

    max_len = 16  # 8 UID bytes -> 16 hex chars

    def _chain(self, m):
        m.submodules.reader = reader = FlashUID()
        m.submodules.encoder = encoder = HexNibbleEncoder()
        connect(m, reader.data, encoder.i)
        return reader, encoder.o


class SecurityPageSerialSource(_SerialSource):
    """Board `uuid` parsed out of the JSON security page"""

    max_len = 36  # canonical UUID length

    def _chain(self, m):
        m.submodules.reader = reader = SecurityPage()
        m.submodules.parser = parser = JsonStringKeyParser(key=b"uuid")
        connect(m, reader.data, parser.i)
        # `done` stops the page read as soon as the value is captured.
        m.d.comb += reader.abort.eq(parser.done)
        return reader, parser.o
