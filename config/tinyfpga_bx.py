"""TinyFPGA BX"""

from amaranth import *
from blocks.lib.ice40.pll import ICE40PLL
from platforms import ICE40Mixin

from amaranth_boards.tinyfpga_bx import TinyFPGABXPlatform as _TinyFPGABXBase
from config import BoardConfig, SerialSource, SLOT1_OFFSET


class TinyFPGABXPlatform(ICE40Mixin, _TinyFPGABXBase):
    """TinyFPGA BX, full-speed: 48 MHz `usb_io` + 12 MHz `sync`.

    The gateware USB PHY samples on the 48 MHz `usb_io` domain; the rest of
    the design runs on `sync`, derived from it by a /4 divider.
    """

    def create_clocks(self, m):
        cd_usb_io = ClockDomain("usb_io")
        cd_sync = ClockDomain("sync")
        m.domains += [cd_usb_io, cd_sync]

        m.submodules.pll = pll = ICE40PLL()
        pll.register_clkin(self.request(self.default_clk, dir="i").i,
                           self.default_clk_frequency)
        pll.create_clkout(cd_usb_io, 48e6)
        self.add_clock_constraint(cd_usb_io.clk, 48e6)
        self.add_clock_constraint(cd_sync.clk, 12e6)

        # 12 MHz sync = usb_io / 4.
        div4 = Signal(range(4))
        m.d.usb_io += div4.eq(div4 + 1)
        m.d.comb += cd_sync.clk.eq(div4[-1])

        self._por(m, clk_domain="usb_io", freq=48e6, locked=pll.locked,
                  reset_domains=[cd_usb_io, cd_sync])


board = BoardConfig(
    name="tinyfpga_bx",
    platform=TinyFPGABXPlatform,
    vid=0x1209,
    pid=0x5af0,
    manufacturer="TinyFPGA",
    product="Bootloader",
    board_id="TinyFPGA-BX-v1",
    model="TinyFPGA BX",
    url="https://tinyfpga.com",
    scsi_vendor="TINYFPGA",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.SECURITY_PAGE,
    reload_slot=1,
    reload_image_offset=SLOT1_OFFSET,
)
