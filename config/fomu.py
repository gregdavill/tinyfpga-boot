"""Fomu PVT (iCE40 UP5K)"""

from amaranth import *
from amaranth_boards.fomu_pvt import FomuPVTPlatform as _FomuBase

from platforms import ICE40Mixin
from config import BoardConfig, SerialSource, SLOT1_OFFSET
from staysource import WriteEnableStaySource


class FomuPVTPlatform(ICE40Mixin, _FomuBase):
    """Fomu PVT, full-speed: 48 MHz `usb_io` + 12 MHz `sync`.

    The on-board 48 MHz oscillator (`clk48`) is used for
    usb_io clk, no PLL needed.
    """

    def create_clocks(self, m):
        cd_usb_io = ClockDomain("usb_io")
        cd_sync = ClockDomain("sync")
        m.domains += [cd_usb_io, cd_sync]

        # clk48 is already 48 MHz on a global buffer; use it as `usb_io`.
        m.d.comb += cd_usb_io.clk.eq(self.request(self.default_clk, dir="i").i)
        self.add_clock_constraint(cd_usb_io.clk, 48e6)
        self.add_clock_constraint(cd_sync.clk, 12e6)

        # 12 MHz sync = usb_io / 4.
        div4 = Signal(range(4))
        m.d.usb_io += div4.eq(div4 + 1)
        m.d.comb += cd_sync.clk.eq(div4[-1])

        self._por(m, clk_domain="usb_io", freq=48e6, locked=Const(1),
                  reset_domains=[cd_usb_io, cd_sync])


board = BoardConfig(
    name="fomu",
    platform=FomuPVTPlatform,
    vid=0x1209,
    pid=0x5bf0,
    manufacturer="Foosn",
    board_id="Fomu-PVT",
    model="Fomu PVT",
    url="https://tomu.im",
    scsi_vendor="FOMU",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.FLASH_UID,
    reload_slot=1,
    reload_image_offset=SLOT1_OFFSET,
    stay_sources=(WriteEnableStaySource,),
)
