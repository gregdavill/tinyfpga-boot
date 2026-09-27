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

from gateware.blocks.qspi.controller import Mode
from gateware.blocks.qspi.flash import QspiFlash, FlashPort
from gateware.blocks.qspi.ram import QspiRam
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
