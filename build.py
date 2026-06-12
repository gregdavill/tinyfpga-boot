import argparse
import subprocess
from pathlib import Path

from config import BOARDS, MULTIBOOT_ALIGN_BITS, SLOT1_OFFSET, Backend
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
            ecppack_opts=(
                f"--bootaddr {bootaddr:#x} "
                "--compress "
                "--spimode qspi --freq 38.8"
            ),
        )

        # The bootloader bitstream lives at flash 0x0; the user image lands at
        # reload_image_offset. If the bootstream is larger than that offset the
        # image region overlaps (and corrupts) the bootloader, so refuse it.
        boot_bit = out / "top.bit"
        boot_size = boot_bit.stat().st_size
        if boot_size > config.reload_image_offset:
            raise SystemExit(
                f"bootloader bitstream is {boot_size:#x} bytes, larger than the "
                f"slot-1 offset {config.reload_image_offset:#x} - the user image "
                f"region would overlap and corrupt the bootloader. Increase "
                f"reload_image_offset (config.ECP5_SLOT1_OFFSET)."
            )

        print(f"built {out}/top")
        return boot_bit

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
    # `-a` (lowercase) leaves image 0 unaligned, so the bootloader sits right
    # after the multiboot header and cold-boot reads it from ~0x0.
    subprocess.run(
        ["icemulti", f"-a{MULTIBOOT_ALIGN_BITS}", "-p0",
         "-o", str(multiboot), str(boot_bin)],
        check=True,
    )
    print(f"wrote {multiboot} (bootloader @ ~0x0, "
          f"slot {config.reload_slot} = user image @ {config.reload_image_offset:#x})")
    return multiboot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build TinyFPGA bootloader bitstream")
    parser.add_argument("--board", default="tinyfpga_bx", choices=BOARDS.keys(),
                        help="Target board (default: tinyfpga_bx)")
    parser.add_argument("--backend", default=None,
                        help="USB personality to build: a single value ('uf2' "
                             "Mass-Storage / drag-and-drop, 'serial' TinyFPGA "
                             "CDC-ACM bridge, 'dfu' USB DFU), a comma-separated "
                             "list to expose several at once as a composite "
                             "device (e.g. 'uf2,dfu,serial'), or 'all'. Defaults "
                             "to the board's config.")
    args = parser.parse_args()

    config = BOARDS[args.board]
    if args.backend:
        if args.backend == "all":
            config.backend = list(Backend)
        elif "," in args.backend:
            config.backend = [Backend(v) for v in args.backend.split(",")]
        else:
            config.backend = Backend(args.backend)

    build(config)
