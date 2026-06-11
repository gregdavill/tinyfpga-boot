"""Re-pack an ECP5 bitstream into per-frame chunks to satisfy the PSRAM tCEM

The ECPBreaker can configure from a QSPI PSRAM, but it's PSRAM enforces a maximum
CS-active time (tCEM, ~8 us). A normal bitstream overruns tCEM.

  * `repack()` unpacks the bitstream and re-emits it with a `JUMP` after *every*
    configuration frame, so each burst stays under tCEM. Note this has a side
    effect of breaking the "golden image" feature on ECP5, a configuration
    timeout/error will retry from the last jump address, rather than default
    address

  * `flash_header()` returns a valid ECP5 header for any density which issues
    a SPI-clock setup and the first `JUMP`. This is placed in FLASH at BOOTADDR
    So that the first slow-SCK/SPI access is not breaking tCEM of the RAM. 
    Even with this minimal 49 byte header this takes ~105us. 

CLI:
    python -m tools.ecp_bitstream repack INPUT.bit OUTPUT.bit
    python -m tools.ecp_bitstream header OUTPUT.bit
"""

import argparse
import logging

from .constants import (
    BitstreamCommand, PREAMBLE, RAM_BOOT_ADDRESS, POISON_WORD,
    SPI_FREQ_MHZ, DEFAULT_SPI_FREQ, prog_cntrl0,
    SPI_MODES, DEFAULT_SPI_MODE,
)
from .parser import BitstreamReader, BitstreamReadWriter

log = logging.getLogger(__name__)


def _insert_jump(bw, target=None, spimode=DEFAULT_SPI_MODE):
    """Emit a JUMP that re-issues the flash read in `spimode`. With `target=None`
    it resumes inline (the next address); with an explicit `target` it jumps there
    (used by the FLASH header to land on the RAM image at the slot address). The
    CS toggle this causes is what latches the ECPBreaker bus over to PSRAM."""
    mode_byte, read_opcode = SPI_MODES[spimode]

    dummy_insert = 0
    if target is None:
        address = (len(bw.data) + 8 + 0x00000)

        # The quad-read mode bits end up set from address[7:4]. The QUAD read
        # fails if they land on 0xA, so if the address would do that, shift it
        # forward by up to 16 with dummy padding.
        while address & 0x20:
            dummy = 0x20 - (address & 0x1F)
            address += dummy
            dummy_insert += dummy
    else:
        address = target

    bw.write_byte(BitstreamCommand.JUMP)
    bw.write_byte(mode_byte)
    bw.write_byte(0xFF)
    bw.write_byte(0xFF)

    bw.write_byte(read_opcode)
    bw.write_byte(address >> 16 & 0xFF)
    bw.write_byte(address >> 8 & 0xFF)
    bw.write_byte(address & 0xFF)

    bw.insert_zeros(4)

    if dummy_insert:
        bw.insert_dummy(dummy_insert)

    # LSCC not needed after a jump, but 0xFF00 + preamble is.
    bw.write_bytes(b'\xFF\x00')
    bw.write_bytes(PREAMBLE)
    bw.insert_dummy(4)


def _write_spi_setup(bw, cntrl0, poison=None):
    """Write the preamble + SPI-clock setup (no JUMP). Shared by the FLASH
    header and the repacked image. `poison`, if given, is written before the
    preamble so the bootloader's staysource treats the slot as no-valid-app
    (the ECP5 config scans past it to the preamble)."""
    bw.write_bytes(b'LSCC')
    bw.write_bytes(bytes([0xFF, 0x00]))

    if poison is not None:
        bw.write_u32(poison)

    bw.insert_dummy(1)
    bw.write_bytes(PREAMBLE)
    # Preamble dummy bytes
    bw.insert_dummy(4)

    bw.write_byte(BitstreamCommand.LSC_PROG_CNTRL0)
    bw.insert_zeros(3)
    bw.write_u32(cntrl0)


def flash_header(boot_address: int = RAM_BOOT_ADDRESS,
                 freq: float = DEFAULT_SPI_FREQ,
                 spimode: str = DEFAULT_SPI_MODE) -> bytes:
    """The image-independent header programmed into FLASH at the bootaddr slot.
    On reconfigure it sets the SPI clock (`freq` MHz) and JUMPs to `boot_address`
    in `spimode`; the CS toggle that JUMP causes latches the bus to PSRAM, so it
    lands on the start of the RAM image (a standalone bitstream based at 0)."""
    bw = BitstreamReadWriter()
    _write_spi_setup(bw, prog_cntrl0(freq), poison=POISON_WORD)
    _insert_jump(bw, target=boot_address, spimode=spimode)
    return bytes(bw.data)


def repack(input_path: str, freq: float = DEFAULT_SPI_FREQ,
           spimode: str = DEFAULT_SPI_MODE) -> bytes:
    """Parse `input_path` and return a tCEM-safe (per-frame JUMP) bitstream."""
    log.info(f"Input Bitstream: {input_path}")
    log.info("Parsing input Bitstream")
    br = BitstreamReader(input_path)

    num_frames = len(br.compressed_frames)
    log.info(f"Parsed {num_frames} frames, {len(br.ebr)} EBR block(s)")

    log.info("Preparing output Bitstream")
    bw = BitstreamReadWriter()

    # A standalone bitstream: preamble + SPI setup, then the body. The FLASH
    # header JUMPs to this image's start; the inline jump here continues to the
    # first frame.
    _write_spi_setup(bw, prog_cntrl0(freq))
    _insert_jump(bw, spimode=spimode)

    # Reset CRC
    bw.write_byte(BitstreamCommand.LSC_RESET_CRC)
    bw.insert_zeros(3)
    bw.reset_crc16()

    # Verify ID (preserve the input device's IDCODE)
    bw.write_byte(BitstreamCommand.VERIFY_ID)
    bw.insert_zeros(3)
    bw.write_u32(br.idcode)

    bw.write_byte(BitstreamCommand.LSC_INIT_ADDRESS)
    bw.insert_zeros(3)

    # Load compression dict
    bw.write_byte(BitstreamCommand.LSC_WRITE_COMP_DIC)
    bw.insert_zeros(3)
    bw.write_bytes(bytearray(reversed(br.compression_dict[8:])))

    # Program each frame, followed by a JUMP so CS toggles between frames.
    for i in range(num_frames):
        bw.write_byte(BitstreamCommand.LSC_PROG_INCR_CMP)
        bw.write_byte(0x91)

        # Frame count
        bw.write_u16(1)

        bw.write_bytes(bytearray(br.compressed_frames[i]))
        bw.insert_crc16()
        bw.write_byte(0xFF)

        _insert_jump(bw, spimode=spimode)

        # Reset CRC
        bw.write_byte(BitstreamCommand.LSC_RESET_CRC)
        bw.insert_zeros(3)
        bw.reset_crc16()

    # Load EBR (block RAM) contents.
    for ebr in br.ebr:
        frame_count = len(ebr[1]) // 9
        log.info("ebr: addr={:x}, cnt={}".format(ebr[0], frame_count))

        bw.write_byte(BitstreamCommand.LSC_EBR_ADDRESS)
        bw.insert_zeros(3)
        bw.write_u32(ebr[0])

        bw.write_byte(BitstreamCommand.LSC_EBR_WRITE)
        bw.write_byte(0xd0)  # CRC
        bw.write_u16(frame_count)

        bw.write_bytes(ebr[1])
        bw.insert_crc16()

        _insert_jump(bw, spimode=spimode)

        # Reset CRC
        bw.write_byte(BitstreamCommand.LSC_RESET_CRC)
        bw.insert_zeros(3)
        bw.reset_crc16()

    bw.write_byte(BitstreamCommand.ISC_PROGRAM_DONE)
    bw.insert_zeros(3)

    bw.insert_zeros(512)

    log.info(f"Output Bitstream: {len(bw.data)} bytes")
    return bytes(bw.data)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true",
                        help="log per-frame/EBR detail")
    common.add_argument("--freq", type=float, default=DEFAULT_SPI_FREQ,
                        choices=sorted(SPI_FREQ_MHZ),
                        help="SPI read clock in MHz (default %(default)s; use a ")
    common.add_argument("--spimode", default=DEFAULT_SPI_MODE,
                        choices=list(SPI_MODES),
                        help="JUMP SPI read mode (default %(default)s; fast-read ")

    parser = argparse.ArgumentParser(
        prog="python -m tools.ecp_bitstream",
        description="Re-pack an ECP5 bitstream for QSPI-PSRAM configuration "
                    "(per-frame JUMP, tCEM-safe) and emit the FLASH jump header.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("repack", parents=[common],
                        help="re-pack a .bit into the tCEM-safe RAM image")
    rp.add_argument("input", help="input ECP5 .bit")
    rp.add_argument("output", help="output (re-packed) .bit")

    hp = sub.add_parser("header", parents=[common],
                        help="write the image-independent FLASH jump header")
    hp.add_argument("output", help="output header .bit")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s - %(levelname)s - %(message)s")

    data = (repack(args.input, freq=args.freq, spimode=args.spimode)
            if args.cmd == "repack"
            else flash_header(freq=args.freq, spimode=args.spimode))
    with open(args.output, "wb") as f:
        f.write(data)
    print(f"wrote {args.output} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
