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

from gateware.blocks.qspi.controller import Mode
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
