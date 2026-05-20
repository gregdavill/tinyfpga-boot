import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from amaranth_boards.tinyfpga_bx import TinyFPGABXPlatform
from top import Top

# --- Multiboot layout ---------------------------------------------------
#
# SB_WARMBOOT selects a slot (S1:S0); the *flash offset* for each slot
# lives in the icemulti multiboot header at flash address 0.
#
# icemulti aligns images to 2^ALIGN_BITS (`-A<n>`). 
# An iCE40 LP8K bitstream needs a 256 KiB slots
MULTIBOOT_ALIGN_BITS = 18                          # 256 KiB slots
SLOT1_OFFSET         = 1 << MULTIBOOT_ALIGN_BITS    # 0x40000


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
        reload_image_offset=SLOT1_OFFSET,
    ),
}

PLATFORMS = {
    "tinyfpga_bx": TinyFPGABXPlatform,
}


def build(config, *, build_dir="build", do_program=False):
    """Build an iCE40 bitstream and assemble a multiboot image.
    """
    if config.reload_slot == 1 and config.reload_image_offset != SLOT1_OFFSET:
        raise SystemExit(
            f"reload_image_offset {config.reload_image_offset:#x} does not "
            f"match the icemulti slot-1 offset {SLOT1_OFFSET:#x} "
            f"(MULTIBOOT_ALIGN_BITS={MULTIBOOT_ALIGN_BITS})."
        )

    out = Path(build_dir)
    PLATFORMS[config.platform]().build(
        Top(config), name="top", build_dir=str(out), do_program=do_program,
    )

    boot_bin = out / "top.bin"
    boot_size = boot_bin.stat().st_size
    if boot_size > SLOT1_OFFSET:
        raise SystemExit(
            f"bootloader bitstream is {boot_size:#x} bytes, larger than the "
            f"slot size {SLOT1_OFFSET:#x}"
        )

    multiboot = out / "multiboot.bin"
    subprocess.run(
        ["icemulti", f"-A{MULTIBOOT_ALIGN_BITS}", "-p0",
         "-o", str(multiboot), str(boot_bin), str(boot_bin)],
        check=True,
    )
    print(f"wrote {multiboot} "
          f"(slot 0 = bootloader @ 0x0, slot 1 = user image @ {SLOT1_OFFSET:#x})")
    return multiboot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build TinyFPGA bootloader bitstream")
    parser.add_argument("--board", default="tinyfpga_bx", choices=BOARDS.keys(),
                        help="Target board (default: tinyfpga_bx)")
    args = parser.parse_args()

    build(BOARDS[args.board])
