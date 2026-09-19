"""Fomu EVT (iCE40 UP5K)"""

from amaranth import *
from amaranth.build import *
from amaranth.vendor import LatticeICE40Platform
from amaranth_boards.resources import *

from platforms import ICE40Mixin
from config import BoardConfig, SerialSource, SLOT1_OFFSET, Backend
from staysource import WriteEnableStaySource, AlwaysStaySource


class FomuEVTPlatform(ICE40Mixin, LatticeICE40Platform):
    """Fomu EVT, full-speed: 48 MHz `usb_io` + 12 MHz `sync`.

    The on-board 48 MHz oscillator (`clk48`) is used for usb_io clk.

    EVT uses the SG48 QFN-48 package, so the pin map is numeric. 
    Since this is an evaluation design amaranth-boards has no platform
    for it.

    The resources below follow the EVT3 pin assignment.
    """

    device      = "iCE40UP5K"
    package     = "SG48"
    default_clk = "clk48"
    resources   = [
        Resource("clk48", 0, Pins("44", dir="i"),
                 Clock(48e6), Attrs(GLOBAL=True, IO_STANDARD="SB_LVCMOS")),

        RGBLEDResource(0,
            r="40", g="41", b="39", invert=True,
            attrs=Attrs(IO_STANDARD="SB_LVCMOS")
        ),

        # The EVT bonds a 1.5 kOhm resistor to *both* data lines
        #  - pin 35 to D+ (full-speed strap) 
        #  - pin 36 to D- (low-speed strap) 
        # Only the D+ one is used by our USB PHY, as `pullup`
        DirectUSBResource(0, d_p="34", d_n="37", pullup="35",
            attrs=Attrs(IO_STANDARD="SB_LVCMOS"),
        ),
        Resource("usb_dn_pullup", 0, Pins("36", dir="oe"),
                 Attrs(IO_STANDARD="SB_LVCMOS", PULLUP=0)),

        # io2/io3 (18/19) are bonded but unused
        *SPIFlashResources(0,
            cs_n="16", clk="15", copi="14", cipo="17",
            attrs=Attrs(IO_STANDARD="SB_LVCMOS"),
        ),
    ]

    connectors = []

    def create_clocks(self, m):
        cd_usb_io = ClockDomain("usb_io")
        cd_sync = ClockDomain("sync")
        m.domains += [cd_usb_io, cd_sync]

        # clk48 used as `usb_io`.
        m.d.comb += cd_usb_io.clk.eq(self.request(self.default_clk, dir="i").i)
        self.add_clock_constraint(cd_usb_io.clk, 48e6)
        self.add_clock_constraint(cd_sync.clk, 12e6)

        # 12 MHz sync = usb_io / 4
        div4 = Signal(range(4))
        m.d.usb_io += div4.eq(div4 + 1)
        m.d.comb += cd_sync.clk.eq(div4[-1])

        self._por(m, clk_domain="usb_io", freq=48e6, locked=Const(1),
                  reset_domains=[cd_usb_io, cd_sync])

    def configure_usb(self, m):
        """Hold the D- strap resistor in true Hi-Z.

        Full speed is signalled by pulling D+ up and leaving D- alone, so the
        pin behind D-'s 1.5 kOhm is driven as HiZ, not left as a default pulldown. 
        """
        dn_pullup = self.request("usb_dn_pullup", 0, dir="oe")
        m.d.comb += [
            dn_pullup.o.eq(0),
            dn_pullup.oe.eq(0),
        ]


board = BoardConfig(
    name="fomu-evt",
    platform=FomuEVTPlatform,
    vid=0x1209,
    pid=0x5bf0,
    manufacturer="Foosn",
    board_id="Fomu-EVT",
    model="Fomu EVT",
    url="https://tomu.im",
    scsi_vendor="FOMU",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.FLASH_UID,
    backend=[Backend.UF2_MSC, Backend.DFU],
    reload_slot=1,
    reload_image_offset=SLOT1_OFFSET,
    stay_sources=(WriteEnableStaySource, AlwaysStaySource),
)
