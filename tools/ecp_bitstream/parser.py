"""ECP5 bitstream parse/build. Based on nextpnr/trellis

`BitstreamReader` unpacks a `.bit` into configuration frames and EBR (block-RAM) contents
`BitstreamReadWriter` packs with a with running CRC-16, used to produce a new bitstream.
"""

import logging
import struct
from enum import Enum

from .constants import (
    BitstreamCommand, PREAMBLE, CRC16_INIT, CRC16_TABLE, DEVICES,
)

log = logging.getLogger(__name__)


class BitstreamReadWriter:
    """Accumulates output bytes while maintaining the bitstream CRC-16."""

    def __init__(self):
        self.crc16 = CRC16_INIT
        self.data = bytearray()

    def update_crc16(self, val):
        self.crc16 = CRC16_TABLE[val ^ (self.crc16 >> 8) & 0xFF] ^ ((self.crc16 << 8) & 0xFFFF)

    def reset_crc16(self):
        self.crc16 = CRC16_INIT

    def insert_crc16(self):
        self.write_u16(self.crc16)
        self.reset_crc16()

    def write_byte(self, val):
        if isinstance(val, Enum):
            val = val.value
        self.data.append(val)
        self.update_crc16(val)

    def write_bytes(self, vals):
        self.data += vals
        for b in vals:
            self.update_crc16(b)

    def write_u32(self, val):
        self.write_bytes(struct.pack('>I', val))

    def write_u16(self, val):
        self.write_bytes(struct.pack('>H', val))

    def insert_zeros(self, count):
        """Insert zero bytes into the bitstream, while updating CRC."""
        for _ in range(count):
            self.write_byte(0)

    def insert_dummy(self, count):
        """Insert dummy (0xFF) bytes into the bitstream, without updating CRC."""
        self.data += bytes([0xFF] * count)


class _Reader:
    """Big-endian reader over a buffered binary stream.

    Thin sugar over the file object so the parser reads as `rd.u32()` /
    `rd.skip(3)` instead of repeating `struct.unpack`/`read` boilerplate.
    """

    def __init__(self, stream):
        self._s = stream

    def read(self, n):
        return self._s.read(n)

    def skip(self, n):
        self._s.read(n)

    def peek(self, n):
        return self._s.peek()[:n]

    def u16(self):
        return struct.unpack(">H", self._s.read(2))[0]

    def u32(self):
        return struct.unpack(">I", self._s.read(4))[0]


class BitstreamReader:
    """Parses an ECP5 `.bit` into `frames`, `compressed_frames` and `ebr`."""

    def __init__(self, filename):
        self.frames = []
        self.compressed_frames = []
        self.ebr = []
        self.idcode = None           # captured from VERIFY_ID
        self.device_name = None
        self.bytes_per_frame = None   # selected from DEVICES by idcode
        self.compression_dict = None  # captured from LSC_WRITE_COMP_DIC

        with open(filename, 'rb') as stream:
            self.rd = _Reader(stream)
            self._seek_preamble()
            self._parse()

    def _seek_preamble(self):
        """Advance past any leading dummy bytes to just after the preamble."""
        rd = self.rd
        while rd.peek(4) != PREAMBLE:
            if not rd.read(1):
                raise ValueError("No Preamble found")
        rd.skip(4)

    def _skip_optional_crc(self, params):
        """Most commands set bit 0x80 of their first param byte when a 2-byte
        CRC check word follows the payload; consume it when present."""
        if params[0] & 0x80:
            self.rd.skip(2)

    def _parse(self):
        rd = self.rd
        while True:
            cur_byte = rd.read(1)
            if not cur_byte:
                break

            # Extract bytes from each opcode as needed.
            op = BitstreamCommand(cur_byte[0])
            match op:
                case BitstreamCommand.DUMMY:
                    pass

                case BitstreamCommand.SPI_MODE:
                    # opcode + 3 param bytes (e.g. 0x59 00 00). The repacker
                    # re-emits SPI setup via LSC_PROG_CNTRL0, so just skip it.
                    rd.skip(3)

                case BitstreamCommand.LSC_RESET_CRC:
                    rd.skip(3)

                case BitstreamCommand.VERIFY_ID:
                    rd.skip(3)
                    self.idcode = rd.u32()
                    if self.idcode not in DEVICES:
                        raise ValueError(f"unknown IDCODE {self.idcode:#010x}")
                    self.device_name, self.bytes_per_frame = DEVICES[self.idcode]
                    log.info("device %s (idcode %#010x), %d B/frame",
                             self.device_name, self.idcode, self.bytes_per_frame)

                case BitstreamCommand.LSC_PROG_CNTRL0:
                    rd.skip(3)
                    rd.skip(4)  # u32 CNTRL0

                case BitstreamCommand.ISC_PROGRAM_USERCODE:
                    params = rd.read(3)
                    rd.skip(4)  # u32 USERCODE
                    self._skip_optional_crc(params)

                case BitstreamCommand.LSC_INIT_ADDRESS:
                    rd.skip(3)

                case BitstreamCommand.ISC_PROGRAM_DONE:
                    params = rd.read(3)
                    self._skip_optional_crc(params)

                case BitstreamCommand.LSC_WRITE_COMP_DIC:
                    params = rd.read(3)
                    dic = rd.read(8)
                    self.compression_dict = [1 << i for i in range(8)] + [b for b in reversed(dic)]
                    self._skip_optional_crc(params)

                case BitstreamCommand.LSC_PROG_INCR_CMP:
                    self._read_compressed_frames()

                case BitstreamCommand.LSC_EBR_ADDRESS:
                    params = rd.read(3)
                    addr = rd.u32()
                    log.info("LSC_EBR_ADDRESS: {:08x} {}".format(addr, params))
                    self.ebr.append(addr)

                case BitstreamCommand.LSC_EBR_WRITE:
                    params = rd.read(3)
                    frame_count = struct.unpack(">H", params[1:])[0]
                    log.info("LSC_EBR_WRITE: frame_count={:08x} {}".format(frame_count, params))

                    # Skip over frame contents:
                    frame_data = rd.read(9 * frame_count)
                    self.ebr[-1] = (self.ebr[-1], frame_data)
                    self._skip_optional_crc(params)

                case _:
                    raise ValueError(f"Unhandled opcode {op}")

    def _read_compressed_frames(self):
        """Handle LSC_PROG_INCR_CMP: read and decompress `frame_count` frames."""
        rd = self.rd
        if self.bytes_per_frame is None:
            raise ValueError("LSC_PROG_INCR_CMP before VERIFY_ID "
                             "(device frame size unknown)")
        bpf = self.bytes_per_frame

        params = rd.read(3)
        dummy_bytes = params[0] & 0xF
        frame_count = params[1] << 8 | params[2]

        check_crc = params[0] & 0x80
        check_crc_each_frame = params[0] & 0x40 == 0

        # Compressed frames align to 64 bits, padded with 0.
        padded_bytes_per_frame = bpf + (7 - ((bpf - 1) % 8))

        for i in range(frame_count):
            frame, compressed_frame = self._decompress_frame(padded_bytes_per_frame)
            if check_crc_each_frame | (check_crc & (i == frame_count - 1)):
                rd.skip(2)
            rd.skip(dummy_bytes)

            # Trim padding off
            frame = frame[-bpf:]

            self.compressed_frames.append(compressed_frame)
            self.frames.append(frame)

    def _decompress_frame(self, padded_bytes_per_frame):
        rd = self.rd
        read_data = 0
        remaining_bits = 0
        frame = bytearray()
        compressed_frame = bytearray()

        for _ in range(padded_bytes_per_frame):
            if remaining_bits == 0:
                compressed_frame += rd.read(1)
                read_data = compressed_frame[-1]
                remaining_bits = 8

            next_bit = bool(read_data >> (remaining_bits - 1) & 1)
            remaining_bits -= 1

            if next_bit:
                if remaining_bits < 5:
                    compressed_frame += rd.read(1)
                    read_data = (read_data << 8) | compressed_frame[-1]
                    remaining_bits += 8

                next_bit = bool(read_data >> (remaining_bits - 1) & 1)
                remaining_bits -= 1

                if next_bit:
                    if remaining_bits < 8:
                        compressed_frame += rd.read(1)
                        read_data = (read_data << 8) | compressed_frame[-1]
                        remaining_bits += 8
                    frame.append((read_data >> (remaining_bits - 8)) & 0xff)
                    remaining_bits -= 8
                else:
                    idx = ((read_data >> (remaining_bits - 4)) & 0xf)
                    remaining_bits -= 4
                    frame.append(self.compression_dict[idx])
            else:
                frame.append(0)

        return frame, compressed_frame
