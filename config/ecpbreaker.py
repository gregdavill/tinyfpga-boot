"""ECPBreaker r3.0 (ECP5)"""

from amaranth import *
from amaranth.build import *
from amaranth.vendor import LatticeECP5Platform
from amaranth_boards.resources import *

from platforms import ECP5Mixin
from config import BoardConfig, SerialSource, ECP5_SLOT1_OFFSET
from staysource import ButtonStaySource, WriteEnableStaySource

class ECPBreakerR3_0Platform(ECP5Mixin, LatticeECP5Platform):
    """ECPBreaker r3.0 (ECP5 LFE5U-25F)

    """
    device      = "LFE5U-25F"
    package     = "BG381"
    speed       = "8"
    default_clk = "clk25"

    fpga_family = "ecp5"
    usb_phy     = "ulpi_hs"
    flash_clk   = "pad"

    default_usb_connection = "ulpi"

    # # Drop the board's stock `spi_flash` (re-declared below) and `program`
    # # (re-exposed as the open-drain `reconfigure` line).
    resources   = [
        Resource("clk25", 0, Pins("F2", dir="i"), Clock(25e6), Attrs(IO_TYPE="LVCMOS18")),
        
        # IO pin connected to ECP5's PROGRAMN pin to initiate a reconfigure cycle
        Resource("reconfigure", 0, PinsN("T2", dir="o"), Attrs(IO_TYPE="LVCMOS33", OPENDRAIN="ON")),

        # Resource("rst", 0, PinsN("R20", dir="i"), Attrs(IO_TYPE="LVCMOS33")),
        Resource("button", 0, PinsN("R20", dir="i"), Attrs(IO_TYPE="LVCMOS33")),

        Resource("led_rgb_multiplex", 0,
            Subsignal("a", Pins("U19 P20 T19 U20 T20 P19", dir="o")),
            Subsignal("c", Pins("N19 N20 M18", dir="o")),
            Attrs(IO_TYPE="LVCMOS33")
        ),

        Resource('spi_flash_4x', 0,
            Subsignal('cs', PinsN('R2')),
            # CLK connected to U3 (CCLK) on PCB (enables use of DDR/better timing than CCLK)
            Subsignal('clk', Pins('T1')),
            Subsignal('dq', Pins('W2 V2 Y2 W1')),
            Attrs(IO_TYPE="LVCMOS33")
        ),

        # FPGA drive into buffer, with feedback to latch FLASH or RAM as configuration source
        Resource("cfg_ctrl", 0, Pins("R1", dir="io"), Attrs(IO_TYPE="LVCMOS33")),

        # USB3343 HS PHY
        ULPIResource("ulpi", 0,
            data="P18 R18 T18 C11 T17 U16 U17 U18", # Reworked for 25F
            # data="P18 R18 T18 T16 T17 U16 U17 U18", # Original mapping
            clk="N16", clk_dir="o", dir="P17", nxt="N18",
            stp="N17", rst="L16", rst_invert=True,
            attrs=Attrs(IO_TYPE="LVCMOS33", SLEWRATE="SLOW")),

        # HyperRAM
        Resource("ram", 0,
            # Subsignal("clk",   DiffPairs("F4", "E3", dir="o"), Attrs(IO_TYPE="LVCMOS18D")), # DiffPairs seems to have some issues. Maybe in Amaranth or NextPnr
            Subsignal("clk_p",   Pins("F4", dir="o"), Attrs(IO_TYPE="LVCMOS18")),
            Subsignal("clk_n",   Pins("E3", dir="o"), Attrs(IO_TYPE="LVCMOS18")),
            Subsignal("dq",    Pins("A4 A3 B4 D3 B3 C3 A5 B5", dir="io")),
            Subsignal("rwds",  Pins( "E4", dir="io")),
            Subsignal("cs",    PinsN("C5", dir="o")),
            Subsignal("reset", PinsN("A2", dir="o")),
            Attrs(IO_TYPE="LVCMOS18", SLEWRATE="FAST")
        ),

        Resource("vccio_en", 0, Pins("A13", dir="o"), Attrs(IO_TYPE="LVCMOS33")),
        I2CResource("i2c", 0, 
            scl="D11", 
            sda="D12", 
            attrs=Attrs(IO_TYPE="LVCMOS33", SLEWRATE="SLOW")
        ),
    ]

    connectors = []


board = BoardConfig(
    name="ecpbreaker",
    platform=ECPBreakerR3_0Platform,
    vid=0x1209,
    pid=0x5af0,
    manufacturer="GsD/GroupGets",
    board_id="ecpbreaker-r3.0",
    model="ECPBreaker r3.0",
    url="https://gregdavill.com",
    scsi_vendor="ECPBREAKER",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.FLASH_UID,
    reload_slot=1,
    reload_image_offset=ECP5_SLOT1_OFFSET,
    stay_sources=(ButtonStaySource, WriteEnableStaySource),
)
