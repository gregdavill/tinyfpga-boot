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

from .qspi import Mode
# Reuse the flash opcodes rather than re-defining them.
from .flash import WRITE_ENABLE, SECTOR_ERASE, PAGE_PROGRAM, READ_STATUS

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
            shape=8, depth=N, init=self._header)
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


# Test cases
import unittest
from .test_util import simulate


def _run(header, readback, *, max_cycles=4000):
    """Drive a BootHeader with `qo` always accepted, feeding `readback` bytes
    back on `qi` whenever the DUT captures (its READ/POLL capture states).

    Returns (octets, done_seen) where octets is the list of (chip, mode, data)
    the DUT emitted on `qo`."""
    dut = BootHeader(header_bytes=header, base_addr=0x200000)
    octets = []
    ri = 0
    done_seen = False

    async def tb(ctx):
        nonlocal ri, done_seen
        ctx.set(dut.qo.ready, 1)
        ctx.set(dut.i_start, 1)
        await ctx.tick()
        ctx.set(dut.i_start, 0)
        for _ in range(max_cycles):
            cap = ctx.get(dut.qi.ready)
            if cap and ri < len(readback):
                ctx.set(dut.qi.valid, 1)
                ctx.set(dut.qi.p.data, readback[ri])
            else:
                ctx.set(dut.qi.valid, 1 if cap else 0)
                ctx.set(dut.qi.p.data, 0)
            if ctx.get(dut.qo.valid):
                octets.append((ctx.get(dut.qo.p.chip),
                               ctx.get(dut.qo.p.mode),
                               ctx.get(dut.qo.p.data)))
            consumed = cap and ctx.get(dut.qi.valid)
            await ctx.tick()
            if consumed and ri < len(readback):
                ri += 1
            if ctx.get(dut.done):
                done_seen = True
                break

    simulate(dut, tb)
    return octets, done_seen


_HDR = bytes([0x4C, 0x53, 0x43, 0x43] + list(range(45)))  # 49 bytes, like flash_header
_SLOT_ADDR = [0x20, 0x00, 0x00]   # 0x200000, MSB first


def _bursts(octets):
    """Parse the (chip, mode, data) octet stream into CS-framed bursts. A burst
    is the run of chip==1 octets between CS releases (chip==0 Dummy); its first
    PutX1 byte is the command opcode (addr/data bytes follow). This is the only
    correct way to read opcodes back out — raw byte values collide (the slot
    addr's high byte 0x20 == SECTOR_ERASE, header data contains 0x06 == WREN)."""
    bursts, cur = [], None
    for (chip, mode, d) in octets:
        if chip == 0:                      # CS release ends a burst
            if cur is not None:
                bursts.append(cur); cur = None
            continue
        if mode == Mode.PutX1:
            if cur is None:
                cur = {"op": d, "data": [], "gets": 0}
            else:
                cur["data"].append(d)
        elif mode == Mode.GetX1:
            if cur is not None:
                cur["gets"] += 1
    if cur is not None:
        bursts.append(cur)
    return bursts


class TestBootHeader(unittest.TestCase):
    def test_match_skips_program(self):
        """Slot already holds the header → read-compare matches → no erase or
        program, just the single read burst, then done."""
        octets, done = _run(_HDR, _HDR)
        self.assertTrue(done, "never completed")
        bursts = _bursts(octets)

        # Exactly one burst: READ + 3 addr + N GetX1.
        self.assertEqual([b["op"] for b in bursts], [READ])
        self.assertEqual(bursts[0]["data"], _SLOT_ADDR)
        self.assertEqual(bursts[0]["gets"], len(_HDR), "one GetX1 per header byte")

    def test_mismatch_erases_and_programs(self):
        """Slot reads erased (0xFF) → mismatch → WREN/erase/poll, then
        WREN/program (the 49 header bytes)/poll, then done."""
        readback = [0xFF] * len(_HDR) + [0x00] * 8   # status polls read WIP=0
        octets, done = _run(_HDR, readback)
        self.assertTrue(done, "never completed")
        bursts = _bursts(octets)
        ops = [b["op"] for b in bursts]

        # READ, then WREN+ERASE+POLL, then WREN+PROGRAM+POLL.
        self.assertEqual(ops[0], READ)
        self.assertEqual(ops.count(WRITE_ENABLE), 2, "one WREN before erase, one before program")
        self.assertEqual(ops.count(SECTOR_ERASE), 1)
        self.assertEqual(ops.count(PAGE_PROGRAM), 1)
        self.assertLess(ops.index(SECTOR_ERASE), ops.index(PAGE_PROGRAM), "erase before program")

        # Erase and program both target the slot address.
        erase = bursts[ops.index(SECTOR_ERASE)]
        pp    = bursts[ops.index(PAGE_PROGRAM)]
        self.assertEqual(erase["data"], _SLOT_ADDR)
        self.assertEqual(pp["data"][:3], _SLOT_ADDR)
        # The programmed payload (after the 3 addr bytes) is exactly the header.
        self.assertEqual(bytes(pp["data"][3:]), _HDR, "programmed bytes != header")

    def test_mismatch_one_byte(self):
        """A single differing byte still triggers a reprogram."""
        rb = bytearray(_HDR)
        rb[10] ^= 0xFF
        readback = list(rb) + [0x00] * 8
        octets, done = _run(_HDR, readback)
        self.assertTrue(done)
        self.assertIn(PAGE_PROGRAM, [b["op"] for b in _bursts(octets)],
                      "a one-byte diff must reprogram")

    def test_wip_retry(self):
        """POLL loops while status bit0 (WIP) is set, then proceeds."""
        # erased read, then erase-poll returns WIP=1 twice then 0, program-poll 0.
        readback = [0xFF] * len(_HDR) + [0x01, 0x01, 0x00] + [0x00] * 8
        octets, done = _run(_HDR, readback)
        self.assertTrue(done, "WIP retry should eventually complete")
        polls = sum(1 for b in _bursts(octets) if b["op"] == READ_STATUS)
        self.assertGreaterEqual(polls, 3, "should poll multiple times")


if __name__ == "__main__":
    unittest.main()
