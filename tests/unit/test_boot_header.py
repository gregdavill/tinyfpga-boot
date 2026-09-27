import unittest
from tests.unit.util import simulate
from gateware.blocks.qspi.controller import Mode
from gateware.blocks.qspi.flash import PAGE_PROGRAM, READ_STATUS, SECTOR_ERASE, WRITE_ENABLE
from gateware.blocks.boot.boot_header import BootHeader, READ


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
