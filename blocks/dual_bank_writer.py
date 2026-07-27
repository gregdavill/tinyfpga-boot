"""Dual-bank (FLASH / QSPI-PSRAM) write datapath.

Takes a single (addr, data) write stream plus a `ram_select` line and routes the
transfer to either the FLASH writer (`QspiFlash`) or the PSRAM writer
(`QspiRam`), driving the `cfg_ctrl` bank latch and the tCEM watchdog
(`DualBank`). For a RAM transfer it first ensures the FLASH boot header is
present in the slot.
"""

from amaranth import *
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out, flipped

from .qspi import Mode
from .flash import QspiFlash, FlashPort
from .qspi_ram import QspiRam
from .dual_bank import DualBank
from .boot_header import BootHeader


_QO_LAYOUT = data.StructLayout({"chip": range(2), "mode": Mode, "data": 8})
_QI_LAYOUT = data.StructLayout({"data": 8})

# tCEM watchdog threshold. 400 cycles is ~6.7 us at 60 MHz, below the APS6404L's
# 8 us max CS-active time.
_RAM_TCEM_CYCLES = 400


class DualBankWriter(wiring.Component):
    def __init__(self, *, header_bytes: bytes, base_addr: int,
                 tcem_cycles: int = _RAM_TCEM_CYCLES):
        self.flash       = QspiFlash()
        self.ram         = QspiRam()
        self.dual_bank   = DualBank(max_cs_cycles=tcem_cycles)
        self.boot_header = BootHeader(header_bytes=header_bytes, base_addr=base_addr)

        super().__init__({
            # Write side: addr/data stream (addr already correct for the
            # selected bank) + flush + ram_select (route this transfer to RAM).
            "port":       In(FlashPort),
            "clear":      In(1),     # reset the header-ensured latch (new session)

            # Muxed QSPI bus, exposed to Top.
            "qo":         Out(stream.Signature(_QO_LAYOUT)),
            "qi":         In(stream.Signature(_QI_LAYOUT)),

            "cfg_ctrl_o": Out(1),    # FLASH/RAM bank latch
            "arm":        Out(1),    # reconfigure arm
            "active":     Out(1),    # any writer driving the bus (for activity/status)
        })

    def elaborate(self, platform) -> Module:
        m = Module()
        m.submodules.flash = flash = self.flash
        m.submodules.ram = ram = self.ram
        m.submodules.dual_bank = db = self.dual_bank
        m.submodules.boot_header = bh = self.boot_header

        # Before the first RAM byte, ensure the FLASH slot holds the boot header.
        # Easiest to do this before switching to the RAM
        header_done = Signal()
        ensuring    = Signal()
        m.d.comb += ensuring.eq(self.port.ram_select & ~header_done)

        with m.FSM(name="header"):
            with m.State("WAIT"):
                with m.If(ensuring & ram.idle):
                    m.d.comb += bh.i_start.eq(1)
                    m.next = "RUN"
            with m.State("RUN"):
                with m.If(bh.done):
                    m.d.sync += header_done.eq(1)
                    m.next = "WAIT"
        with m.If(self.clear):
            m.d.sync += header_done.eq(0)

        # Route the write stream to the selected writer (one pre-corrected addr).
        m.d.comb += [
            flash.port.w.p.eq(self.port.w.p),
            ram.i.p.eq(self.port.w.p),
            flash.port.w.valid.eq(self.port.w.valid & ~self.port.ram_select),
            ram.i.valid.eq(self.port.w.valid & self.port.ram_select & ~ensuring),
            flash.port.flush.eq(self.port.flush),
            ram.done.eq(self.port.flush),
        ]
        with m.If(ensuring):
            m.d.comb += self.port.w.ready.eq(0)
        with m.Elif(self.port.ram_select):
            m.d.comb += self.port.w.ready.eq(ram.i.ready)
        with m.Else():
            m.d.comb += self.port.w.ready.eq(flash.port.w.ready)

        # Bank-select + tCEM handshake.
        m.d.comb += [
            db.bank.eq(ram.bank),
            db.cs_open.eq(ram.cs_open),
            ram.tcem_expired.eq(db.tcem_expired),
            self.cfg_ctrl_o.eq(db.cfg_ctrl_o),
        ]

        # Bus mux: BootHeader during the ensure, else the selected writer.
        with m.If(ensuring):
            wiring.connect(m, flipped(self.qo), bh.qo)
            wiring.connect(m, flipped(self.qi), bh.qi)
        with m.Elif(self.port.ram_select):
            wiring.connect(m, flipped(self.qo), ram.qo)
            wiring.connect(m, flipped(self.qi), ram.qi)
        with m.Else():
            wiring.connect(m, flipped(self.qo), flash.qo)
            wiring.connect(m, flipped(self.qi), flash.qi)

        # RAM transfers reconfigure after the writer's boot-arm; FLASH on `flush`.
        m.d.comb += [
            self.arm.eq(Mux(self.port.ram_select, ram.boot_ready, self.port.flush)),
            self.active.eq(flash.qo.valid | ram.qo.valid | bh.qo.valid),
        ]

        return m


# Test cases
import unittest
from .test_util import simulate
from .boot_header import READ
from .flash import WRITE_ENABLE, PAGE_PROGRAM
from .qspi_ram import ENTER_QPI, WRITE as RAM_WRITE


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
