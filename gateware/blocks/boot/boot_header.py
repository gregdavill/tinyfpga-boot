"""FLASH boot-header writer.

QSPI RAMs large enough to hold a bitstream use DRAM internally. They
impose a maximum CS enable time, During IDLE they run an internal
self-refresh cycle. This max CS time is typically ~8us (less
for extended temp ranges).

To meet this, the ECP5 config engine needs to be primed into QSPI
mode with fater SCLK. So a valid bitstream header is placed in FLASH
this block ensure this header is present, programming it if needed.
"""

from amaranth import *
from amaranth.lib import wiring, stream, data, enum, memory
from amaranth.lib.wiring import In, Out

from gateware.blocks.qspi.controller import Mode
# Reuse the flash opcodes rather than re-defining them.
from gateware.blocks.qspi.flash import WRITE_ENABLE, SECTOR_ERASE, PAGE_PROGRAM, READ_STATUS

READ = 0x03   # single-I/O read, no dummy byte


class _Poll(enum.Enum, shape=1):
    """Where the WIP poll returns after status clears."""
    PROGRAM = 0   # erase finished → go program
    DONE    = 1   # program finished → done


class BootHeader(wiring.Component):
    def __init__(self, *, header_bytes: bytes, base_addr: int):
        self._header = bytes(header_bytes)
        self._n = len(self._header)
        self._base = base_addr
        super().__init__({
            "i_start": In(1),    # pulse to begin an ensure
            "busy":    Out(1),   # high while running
            "done":    Out(1),   # one-cycle pulse on completion

            "qo": Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "qi": In(stream.Signature(data.StructLayout({"data": 8}))),
        })

    def elaborate(self, platform) -> Module:
        m = Module()
        N = self._n

        m.submodules.rom = rom = memory.Memory(
            shape=8, depth=N, init=self._header, attrs={"ram_style": "block"})
        rp = rom.read_port(domain="sync")

        idx        = Signal(range(N + 1))
        addr_count = Signal(range(3))
        mismatch   = Signal()
        poll_ret   = Signal(_Poll)
        status     = Signal(8)

        rom_adv = Signal()
        m.d.comb += [
            rp.en.eq(1),
            rp.addr.eq(idx + rom_adv),
        ]

        # The writer issues no reads it doesn't capture, but keep qi drained.
        m.d.comb += self.qi.ready.eq(0)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.qo.p.chip.eq(chip),
                self.qo.p.mode.eq(mode),
                self.qo.p.data.eq(d),
                self.qo.valid.eq(1),
            ]

        # The 3 address bytes of the (constant) slot base, MSB first.
        addr_byte = Signal(8)
        with m.Switch(addr_count):
            with m.Case(0):
                m.d.comb += addr_byte.eq((self._base >> 16) & 0xFF)
            with m.Case(1):
                m.d.comb += addr_byte.eq((self._base >> 8) & 0xFF)
            with m.Default():
                m.d.comb += addr_byte.eq(self._base & 0xFF)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.i_start):
                    m.d.sync += [mismatch.eq(0), idx.eq(0), addr_count.eq(0)]
                    m.next = "READ_CMD"

            # --- Read the slot back (0x03 + 3 addr, then N bytes), compare. ---
            with m.State("READ_CMD"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, READ)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "READ_ADDR"

            with m.State("READ_ADDR"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.d.sync += idx.eq(0)
                        m.next = "READ_GET"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("READ_GET"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.GetX1)
                with m.If(self.qo.ready):
                    m.next = "READ_CAP"

            with m.State("READ_CAP"):
                m.d.comb += self.busy.eq(1)
                m.d.comb += self.qi.ready.eq(1)
                with m.If(self.qi.valid):
                    with m.If(self.qi.p.data != rp.data):
                        m.d.sync += mismatch.eq(1)
                    with m.If(idx == N - 1):
                        m.next = "READ_DONE"
                    with m.Else():
                        m.d.comb += rom_adv.eq(1)
                        m.d.sync += idx.eq(idx + 1)
                        m.next = "READ_GET"

            with m.State("READ_DONE"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)               # CS release
                with m.If(self.qo.ready):
                    with m.If(mismatch):
                        m.next = "WREN_E"
                    with m.Else():
                        m.next = "DONE"

            # --- Erase the slot sector (only reached on mismatch). ---
            with m.State("WREN_E"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, WRITE_ENABLE)
                with m.If(self.qo.ready):
                    m.next = "WREN_E_REL"

            with m.State("WREN_E_REL"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "ERASE_CMD"

            with m.State("ERASE_CMD"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, SECTOR_ERASE)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "ERASE_ADDR"

            with m.State("ERASE_ADDR"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.next = "ERASE_REL"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("ERASE_REL"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += poll_ret.eq(_Poll.PROGRAM)
                    m.next = "POLL_CMD"

            # --- Poll WIP until the erase/program completes. ---
            with m.State("POLL_CMD"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, READ_STATUS)
                with m.If(self.qo.ready):
                    m.next = "POLL_GET"

            with m.State("POLL_GET"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.GetX1)
                with m.If(self.qo.ready):
                    m.next = "POLL_CAP"

            with m.State("POLL_CAP"):
                m.d.comb += self.busy.eq(1)
                m.d.comb += self.qi.ready.eq(1)
                with m.If(self.qi.valid):
                    m.d.sync += status.eq(self.qi.p.data)
                    m.next = "POLL_REL"

            with m.State("POLL_REL"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(status[0]):          # WIP still set → poll again
                        m.next = "POLL_CMD"
                    with m.Elif(poll_ret == _Poll.PROGRAM):
                        m.next = "WREN_P"
                    with m.Else():
                        m.next = "DONE"

            # --- Program the header (one page, sector freshly erased). ---
            with m.State("WREN_P"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, WRITE_ENABLE)
                with m.If(self.qo.ready):
                    m.next = "WREN_P_REL"

            with m.State("WREN_P_REL"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "PP_CMD"

            with m.State("PP_CMD"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, PAGE_PROGRAM)
                with m.If(self.qo.ready):
                    # Zero idx now so header[0] is prefetched before PP_DATA.
                    m.d.sync += [addr_count.eq(0), idx.eq(0)]
                    m.next = "PP_ADDR"

            with m.State("PP_ADDR"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.next = "PP_DATA"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("PP_DATA"):
                m.d.comb += self.busy.eq(1)
                send(1, Mode.PutX1, rp.data)
                with m.If(self.qo.ready):
                    with m.If(idx == N - 1):
                        m.next = "PP_REL"
                    with m.Else():
                        m.d.comb += rom_adv.eq(1)
                        m.d.sync += idx.eq(idx + 1)

            with m.State("PP_REL"):
                m.d.comb += self.busy.eq(1)
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += poll_ret.eq(_Poll.DONE)
                    m.next = "POLL_CMD"

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"

        return m
