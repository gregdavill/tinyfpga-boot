"""Baseline stay source: stay put unless slot 1 holds a recognisable bitstream.

Reads the first `probe_len` bytes of the application slot over QSPI at boot and
scans for the FPGA family's bitstream preamble/sync word:

  * iCE40 : 0x7EAA997E
  * ECP5  : 0xFFFFBDB3

"""

from amaranth import Module, Signal, Cat

from gateware.blocks.qspi.controller import Mode
from . import StaySource


FAST_READ = 0x0B

# Bitstream sync words by FPGA family, as they appear MSB-first in flash.
SYNC_WORDS = {
    "ice40": 0x7EAA997E,
    "ecp5":  0xFFFFBDB3,
}

# A "poison" word the FLASH jump-header places *before* its preamble so this
# source treats the slot as no-valid-app (stay) even though a real preamble
# follows. The ECP5 config scans past it to the preamble.
POISON_WORD = 0x5AFEB007


class NoValidAppStaySource(StaySource):
    needs_flash = True

    def __init__(self, *, app_offset, sync_word, poison_word=None, probe_len=256):
        self._app_offset = app_offset
        self._sync_word = sync_word
        self._poison_word = poison_word
        self._probe_len = probe_len
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        sync_found = Signal()            # set once the sync word is seen
        poison_found = Signal()          # set if the poison word is seen
        window = Signal(32)              # sliding 4-byte window of read data
        cnt = Signal(range(self._probe_len + 1))
        ai = Signal(range(3))
        addr = self._app_offset

        # `stay` is the no-app verdict; only sampled by Top after `done`.
        m.d.comb += self.stay.eq(~sync_found | poison_found)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.o.p.chip.eq(chip),
                self.o.p.mode.eq(mode),
                self.o.p.data.eq(d),
                self.o.valid.eq(1),
            ]

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.req):
                    m.next = "CMD"

            with m.State("CMD"):
                send(1, Mode.PutX1, FAST_READ)
                with m.If(self.o.ready):
                    m.d.sync += ai.eq(0)
                    m.next = "ADDR"

            with m.State("ADDR"):
                addr_byte = Signal(8)
                with m.Switch(ai):
                    with m.Case(0):
                        m.d.comb += addr_byte.eq((addr >> 16) & 0xFF)
                    with m.Case(1):
                        m.d.comb += addr_byte.eq((addr >> 8) & 0xFF)
                    with m.Default():
                        m.d.comb += addr_byte.eq(addr & 0xFF)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.o.ready):
                    with m.If(ai == 2):
                        m.next = "DUMMY"
                    with m.Else():
                        m.d.sync += ai.eq(ai + 1)

            with m.State("DUMMY"):
                # One dummy byte after the 24-bit address for 0x0B fast read.
                send(1, Mode.PutX1, 0)
                with m.If(self.o.ready):
                    m.d.sync += cnt.eq(self._probe_len)
                    m.next = "READ_PUSH"

            with m.State("READ_PUSH"):
                send(1, Mode.GetX1, 0)
                with m.If(self.o.ready):
                    m.next = "READ_CAP"

            with m.State("READ_CAP"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    # Shift the byte into the low end (MSB-first stream).
                    nxt = Cat(self.i.p.data, window)[:32]
                    m.d.sync += window.eq(nxt)
                    with m.If(nxt == self._sync_word):
                        m.d.sync += sync_found.eq(1)
                    if self._poison_word is not None:
                        with m.If(nxt == self._poison_word):
                            m.d.sync += poison_found.eq(1)
                    with m.If(cnt == 1):
                        m.next = "RELEASE"
                    with m.Else():
                        m.d.sync += cnt.eq(cnt - 1)
                        m.next = "READ_PUSH"

            with m.State("RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.o.ready):
                    m.next = "DONE"

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)

        return m
