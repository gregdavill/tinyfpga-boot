"""OrangeCrab r0.2 (ECP5)"""

from amaranth import *
from amaranth.build import Resource, Subsignal, Pins, PinsN, Attrs
from amaranth_boards.orangecrab_r0_2 import OrangeCrabR0_2Platform as _OrangeCrabBase

from platforms import ECP5Mixin
from config import BoardConfig, SerialSource, SLOT1_OFFSET
from staysource import ButtonStaySource, WriteEnableStaySource

class OrangeCrabR0_2ULPIPlatform(ECP5Mixin, _OrangeCrabBase):
    """OrangeCrab r0.2 (ECP5 LFE5U-25F)

    """

    fpga_family = "ecp5"
    flash_clk   = "usrmclk"

    # Drop the board's stock `spi_flash` (re-declared below) and `program`
    # (re-exposed as the open-drain `reconfigure` line).
    _base = [r for r in _OrangeCrabBase.resources
             if getattr(r, "name", "") not in ("program",)
             and not getattr(r, "name", "").startswith("spi_flash")]

    resources = _base + [
        # cs + dq on the real flash balls; clk omitted (driven via USRMCLK).
        Resource("spi_flash_4x", 0,
            Subsignal("cs", PinsN("U17", dir="o")),
            Subsignal("dq", Pins("U18 T18 R18 N18", dir="io")),
            Attrs(IO_TYPE="LVCMOS33"),
        ),
        # PROGRAMN (V17): pulling it low reloads the FPGA from flash.
        Resource("reconfigure", 0, PinsN("V17", dir="o"),
                 Attrs(IO_TYPE="LVCMOS33", OPENDRAIN="ON")),
    ]


board = BoardConfig(
    name="orangecrab",
    platform=OrangeCrabR0_2ULPIPlatform,
    vid=0x1209,
    pid=0x5af0,
    manufacturer="GsD/GroupGets",
    product="UF2 Bootloader",
    board_id="OrangeCrab-r0.2",
    model="OrangeCrab r0.2",
    url="https://orangecrab-fpga.github.io",
    scsi_vendor="ORANGE",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.SECURITY_PAGE,
    reload_slot=1,
    reload_image_offset=SLOT1_OFFSET,
    stay_sources=(ButtonStaySource, WriteEnableStaySource),
)
