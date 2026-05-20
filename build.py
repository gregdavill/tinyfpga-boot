import argparse
from dataclasses import dataclass

from amaranth_boards.tinyfpga_bx import TinyFPGABXPlatform
from top import Top


@dataclass
class BoardConfig:
    platform: str
    vid: int
    pid: int
    manufacturer: str
    product: str
    board_id: str
    model: str
    url: str
    scsi_vendor: str
    scsi_product: str

    # SB_WARMBOOT slot to reload after a complete UF2 transfer
    reload_slot: int = 1
    # Flash byte offset of the slot's image region. The host's UF2
    # carries addresses relative to 0; the gateware adds this base so
    # the image lands where SB_WARMBOOT(slot) will reconfigure from.
    reload_image_offset: int = 0
    # Sync-domain cycles of quiet required before firing SB_WARMBOOT
    # (default ~50 ms at 12 MHz). None defers to the Warmboot block's 
    # own default
    reload_idle_cycles: int | None = None


BOARDS = {
    "tinyfpga_bx": BoardConfig(
        platform="tinyfpga_bx",
        vid=0x1209,
        pid=0x5af0,
        manufacturer="TinyFPGA",
        product="Bootloader",
        board_id="TinyFPGA-BX-v1",
        model="TinyFPGA BX",
        url="https://tinyfpga.com",
        scsi_vendor="TINYFPGA",
        scsi_product="UF2 Bootloader",
        reload_slot=1,
        reload_image_offset=0x28000,
    ),
}

PLATFORMS = {
    "tinyfpga_bx": TinyFPGABXPlatform,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build TinyFPGA bootloader bitstream")
    parser.add_argument("--board", default="tinyfpga_bx", choices=BOARDS.keys(),
                        help="Target board (default: tinyfpga_bx)")
    args = parser.parse_args()

    config = BOARDS[args.board]
    PLATFORMS[config.platform]().build(Top(config), do_program=False)
