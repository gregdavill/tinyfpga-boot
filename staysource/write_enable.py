"""Write-Enable-Latch stay source.

Uses the SPI flash's volatile Write Enable Latch (WEL, status bit 1) as a
dynamic "stay in the bootloader" request that an application can set without
touching the flash array:

  * The app issues WREN (0x06), setting WEL=1, then triggers a reconfigure.
  * An FPGA reconfigure (PROGRAMN / SB_WARMBOOT) reloads the fabric but does
    NOT power-cycle the flash, so WEL survives into the bootloader.
  * This source reads the status register (0x05) at boot; WEL=1 => stay.

"""

from amaranth import Module, Signal

from blocks.qspi import Mode
from . import StaySource


READ_STATUS = 0x05
WEL = 0x02   # status register bit 1


class WriteEnableStaySource(StaySource):
    needs_flash = True

    def elaborate(self, platform):
        m = Module()

        status = Signal(8)
        # WEL set => stay. Only sampled by Top after `done`.
        m.d.comb += self.stay.eq((status & WEL) != 0)

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
                send(1, Mode.PutX1, READ_STATUS)
                with m.If(self.o.ready):
                    m.next = "READ"

            with m.State("READ"):
                send(1, Mode.GetX1, 0)
                with m.If(self.o.ready):
                    m.next = "CAPTURE"

            with m.State("CAPTURE"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += status.eq(self.i.p.data)
                    m.next = "RELEASE"

            with m.State("RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.o.ready):
                    m.next = "DONE"

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)

        return m


# Test cases
import unittest
from amaranth import Elaboratable
from amaranth.lib import wiring as _wiring, io
from blocks.qspi import Controller, PortGroup
from blocks.test_util import simulate


class TestWriteEnableStaySource(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads

        def elaborate(self, platform):
            m = Module()
            m.submodules.src = src = WriteEnableStaySource()
            m.submodules.qspi = qspi = Controller(self._pads)
            _wiring.connect(m, src.o, qspi.i)
            _wiring.connect(m, src.i, qspi.o)
            m.d.comb += qspi.divisor.eq(1)
            self.src = src
            return m

    def _verdict(self, wel_high):
        pads = PortGroup(
            clk=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            dq=io.SimulationPort("io", 4),
        )
        dut = self.DUT(pads)
        out = {}

        async def tb(ctx):
            # GetX1 samples io1; a constant level makes the status byte 0xFF
            # (WEL set) or 0x00 (WEL clear).
            ctx.set(pads.dq.i, 0b0010 if wel_high else 0b0000)
            ctx.set(dut.src.req, 1)
            for _ in range(2000):
                await ctx.tick()
                if ctx.get(dut.src.done):
                    out["stay"] = ctx.get(dut.src.stay)
                    return

        simulate(dut, tb)
        self.assertIn("stay", out, "reader never asserted done")
        return out["stay"]

    def test_wel_set_stays(self):
        self.assertEqual(self._verdict(wel_high=True), 1)

    def test_wel_clear_boots(self):
        self.assertEqual(self._verdict(wel_high=False), 0)


if __name__ == "__main__":
    unittest.main()
