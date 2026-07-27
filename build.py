import argparse
from pathlib import Path

from config import BOARDS, MULTIBOOT_ALIGN_BITS, SLOT1_OFFSET, Backend
from top import Top


# iCE40 warmboot multiboot header: 5 x 32-byte boot-image records that each name
# a 24-bit flash boot address. The constant bytes are the fixed cold/warm-boot
# preamble.
# Record 0 is the cold-boot image.
# Records 1-4 are four SB_WARMBOOT slots.
_HEADER_LEN = 5 * 32  # 0xA0; the bootloader image is appended right after.


def _multiboot_boot_record(addr):
    return (bytes([0x7E, 0xAA, 0x99, 0x7E, 0x92, 0x00, 0x00, 0x44, 0x03])
            + bytes([(addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF])
            + bytes([0x82, 0x00, 0x00, 0x01, 0x08]) + bytes(15))


def _multiboot_header(*, boot_offset, reload_slot, reload_offset):
    """5-record header pointing the reload warmboot slot at the user image.    """
    addrs = [boot_offset] * 5
    addrs[reload_slot + 1] = reload_offset      # record 0 = cold-boot; +1 = slot
    return b"".join(_multiboot_boot_record(a) for a in addrs)


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
            ecppack_opts=f"--bootaddr {bootaddr:#x} {config.ecppack_opts}",
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
            f"match the multiboot slot-1 offset {SLOT1_OFFSET:#x} "
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

    # The bootloader sits right after the multiboot header, cold-boot reads it
    # from ~0x0; the reload slot points at the user-image region so a warmboot
    # after an upload lands there.
    multiboot = out / "multiboot.bin"
    header = _multiboot_header(
        boot_offset=_HEADER_LEN,
        reload_slot=config.reload_slot,
        reload_offset=config.reload_image_offset,
    )
    multiboot.write_bytes(header + boot_bin.read_bytes())
    print(f"wrote {multiboot} (bootloader @ {_HEADER_LEN:#x}, "
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
