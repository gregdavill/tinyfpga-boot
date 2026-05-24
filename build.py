import argparse
import subprocess
from pathlib import Path

from config import BOARDS, MULTIBOOT_ALIGN_BITS, SLOT1_OFFSET
from top import Top


def build(config, *, build_dir="build", do_program=False):
    """Build a bitstream.
    """
    out = Path(build_dir)

    if config.platform.fpga_family == "ecp5":
        # Configure BOOTADDR to support multi-boot. Used by bootloader to
        # reconfigure the FPGA into the loaded gateware.
        bootaddr = config.reload_image_offset
        config.platform().build(
            Top(config), name="top", build_dir=str(out), do_program=do_program,
            ecppack_opts=f"--bootaddr {bootaddr:#x}",
        )
        print(f"built {out}/top")
        return out / "top.bit"

    if config.reload_slot == 1 and config.reload_image_offset != SLOT1_OFFSET:
        raise SystemExit(
            f"reload_image_offset {config.reload_image_offset:#x} does not "
            f"match the icemulti slot-1 offset {SLOT1_OFFSET:#x} "
            f"(MULTIBOOT_ALIGN_BITS={MULTIBOOT_ALIGN_BITS})."
        )

    config.platform().build(
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
