import unittest
from tests.unit.util import simulate
from gateware.blocks.boot.boot_header import READ
from gateware.blocks.qspi.flash import WRITE_ENABLE, PAGE_PROGRAM
from gateware.blocks.qspi.ram import ENTER_QPI, WRITE as RAM_WRITE
from gateware.blocks.qspi.controller import Mode
from gateware.blocks.boot.dual_bank_writer import DualBankWriter


# A short stand-in header so the BootHeader read/compare is quick in sim.
_HDR = bytes([0x4C, 0x53, 0x43, 0x43])
_BASE = 0x200000


def _bursts(octets):
    """Parse (chip, mode, data) octets into CS-framed bursts; first PutX1 byte
    of each burst is the command opcode."""
    bursts, cur = [], None
    for (chip, mode, d) in octets:
        if chip == 0:                       # CS release ends a burst
            if cur is not None:
                bursts.append(cur); cur = None
            continue
        if cur is None and mode in (Mode.PutX1, Mode.PutX4):
            cur = {"op": d, "mode": mode}   # first command byte = opcode
    if cur is not None:
        bursts.append(cur)
    return bursts


def _run(*, ram_select, readback, ins, max_cycles=3000):
    """Drive a DualBankWriter: present `ins` (list of (addr,data)) on `i`, accept
    qo always, feed `readback` bytes on qi during capture states, pulse `clear`
    first, set `done` once all input is consumed. Returns (octets, reached_arm)."""
    dut = DualBankWriter(header_bytes=_HDR, base_addr=_BASE, tcem_cycles=400)
    octets, ri, idx = [], 0, 0
    reached = False

    async def tb(ctx):
        nonlocal ri, idx, reached
        ctx.set(dut.qo.ready, 1)
        ctx.set(dut.port.ram_select, ram_select)
        ctx.set(dut.clear, 1)
        await ctx.tick()
        ctx.set(dut.clear, 0)
        for _ in range(max_cycles):
            # present next input byte when the writer can take one
            if ctx.get(dut.port.w.ready) and idx < len(ins):
                ctx.set(dut.port.w.valid, 1)
                ctx.set(dut.port.w.p.addr, ins[idx][0])
                ctx.set(dut.port.w.p.data, ins[idx][1])
            else:
                ctx.set(dut.port.w.valid, 0)
            # feed qi during any capture (qi.ready high)
            cap = ctx.get(dut.qi.ready)
            ctx.set(dut.qi.valid, 1 if cap else 0)
            ctx.set(dut.qi.p.data, readback[ri] if (cap and ri < len(readback)) else 0)
            if ctx.get(dut.qo.valid):
                octets.append((ctx.get(dut.qo.p.chip),
                               ctx.get(dut.qo.p.mode), ctx.get(dut.qo.p.data)))
            consumed_i  = ctx.get(dut.port.w.valid) and ctx.get(dut.port.w.ready)
            consumed_qi = cap
            await ctx.tick()
            if consumed_i:
                idx += 1
            if consumed_qi and ri < len(readback):
                ri += 1
            if idx >= len(ins):
                ctx.set(dut.port.flush, 1)
            if ctx.get(dut.arm):
                reached = True
                break

    simulate(dut, tb)
    return octets, reached


class TestDualBankWriter(unittest.TestCase):
    def test_flash_route(self):
        """ram_select=0 routes to the FLASH writer: WREN/program opcodes, never a
        BootHeader read or a QPI-enter; arm follows `done`."""
        octets, reached = _run(
            ram_select=0,
            readback=[0x00] * 64,                       # status polls read WIP=0
            ins=[(0x200000, 0xAA), (0x200001, 0xBB)])
        self.assertTrue(reached, "flash route never armed")
        ops = [b["op"] for b in _bursts(octets)]
        self.assertIn(WRITE_ENABLE, ops)
        self.assertIn(PAGE_PROGRAM, ops)
        self.assertNotIn(READ, ops, "no BootHeader read on the FLASH route")
        self.assertNotIn(ENTER_QPI, ops, "no QPI-enter on the FLASH route")

    def test_ram_route_ensures_header_then_writes(self):
        """ram_select=1 first runs BootHeader (read 0x03) with the input stalled,
        then hands off to QspiRam (QPI-enter 0x35, write 0x02) and boot-arms."""
        # header read-compare matches (no erase/program), then RAM write.
        octets, reached = _run(
            ram_select=1,
            readback=list(_HDR) + [0x00] * 32,
            ins=[(0x000000, 0x11), (0x000001, 0x22)])
        self.assertTrue(reached, "ram route never armed (boot_ready)")
        ops = [b["op"] for b in _bursts(octets)]
        # BootHeader read comes before the PSRAM QPI-enter.
        self.assertIn(READ, ops)
        self.assertIn(ENTER_QPI, ops)
        self.assertIn(RAM_WRITE, ops)
        self.assertLess(ops.index(READ), ops.index(ENTER_QPI),
                        "header ensure must precede the PSRAM write")


if __name__ == "__main__":
    unittest.main()
