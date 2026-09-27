from amaranth import *
from amaranth.lib import data, wiring, stream, memory
from amaranth.lib.wiring import In, Out


from datetime import datetime

SECTOR_SIZE = 512
SECTORS_PER_CLUSTER = 4
RESERVED_SECTORS = 1
NUM_FATS = 2
SECTORS_PER_FAT = 32
ROOT_DIR_ENTRIES = 16
ROOT_DIR_SECTORS = 1

FAT0_SECTOR     = RESERVED_SECTORS                                  # 1
FAT1_SECTOR     = RESERVED_SECTORS + SECTORS_PER_FAT                # 33
ROOT_DIR_SECTOR = RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT     # 65
DATA_START_SECTOR = ROOT_DIR_SECTOR + ROOT_DIR_SECTORS              # 66


def _data_sector(cluster):
    """First LBA of a data cluster (clusters are numbered from 2)."""
    return DATA_START_SECTOR + (cluster - 2) * SECTORS_PER_CLUSTER

_stream_layout = data.StructLayout({"data": 8})


def _le16(value):
    return [value & 0xFF, (value >> 8) & 0xFF]


def _le32(value):
    return [value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF]


def _build_boot_sector(block_count):
    s = [0] * SECTOR_SIZE
    # Jump instruction
    s[0], s[1], s[2] = 0xEB, 0x3C, 0x90
    # OEM name
    s[3:11] = list(b"UF2 UF2 ")
    # BPB
    s[11:13] = _le16(SECTOR_SIZE)           # bytes per sector
    s[13] = SECTORS_PER_CLUSTER             # sectors per cluster
    s[14:16] = _le16(RESERVED_SECTORS)      # reserved sectors
    s[16] = NUM_FATS                        # number of FATs
    s[17:19] = _le16(ROOT_DIR_ENTRIES)      # root dir entries
    s[19:21] = _le16(block_count)           # total sectors (16-bit)
    s[21] = 0xF8                            # media descriptor (fixed disk)
    s[22:24] = _le16(SECTORS_PER_FAT)       # sectors per FAT
    s[24:26] = _le16(1)                     # sectors per track
    s[26:28] = _le16(1)                     # number of heads
    s[28:32] = _le32(0)                     # hidden sectors
    s[32:36] = _le32(0)                     # total sectors (32-bit, 0 since 16-bit is used)
    # Extended boot record
    s[36] = 0x80                            # drive number
    s[37] = 0x00                            # reserved
    s[38] = 0x29                            # extended boot signature
    s[39:43] = _le32(0x00420042)            # volume serial number
    s[43:54] = list(b"UF2 BOOT   ")         # volume label (11 bytes)
    s[54:62] = list(b"FAT16   ")            # filesystem type
    # Boot signature
    s[510] = 0x55
    s[511] = 0xAA
    return s


def _build_fat_sector():
    s = [0] * SECTOR_SIZE
    # Entry 0: media descriptor, Entry 1: end-of-chain, Cluster 2: EOF, Cluster 3: EOF
    entries = [0xFFF8, 0xFFFF, 0xFFFF, 0xFFFF]
    for i, val in enumerate(entries):
        s[i * 2:i * 2 + 2] = _le16(val)
    return s


def _fat_datetime(dt):
    """Encode a datetime as FAT16 date and time words."""
    time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
    return time, date


def _set_dir_entry_timestamps(s, offset, fat_time, fat_date):
    """Set creation and last-write timestamps on a directory entry."""
    s[offset+14:offset+16] = _le16(fat_time)   # creation time
    s[offset+16:offset+18] = _le16(fat_date)   # creation date
    s[offset+22:offset+24] = _le16(fat_time)   # last write time
    s[offset+24:offset+26] = _le16(fat_date)   # last write date


def _build_root_dir(info_size, index_size, build_time):
    fat_time, fat_date = _fat_datetime(build_time)

    s = [0] * SECTOR_SIZE
    # Entry 0: Volume label
    e = 0
    s[e:e+11] = list(b"UF2 BOOT   ")
    s[e+11] = 0x08  # volume label attribute
    e += 32
    # Entry 1: INFO_UF2.TXT
    s[e:e+11] = list(b"INFO_UF2TXT")
    s[e+11] = 0x01  # read-only
    _set_dir_entry_timestamps(s, e, fat_time, fat_date)
    s[e+26:e+28] = _le16(2)  # start cluster
    s[e+28:e+32] = _le32(info_size)
    e += 32
    # Entry 2: INDEX.HTM
    s[e:e+11] = list(b"INDEX   HTM")
    s[e+11] = 0x01  # read-only
    _set_dir_entry_timestamps(s, e, fat_time, fat_date)
    s[e+26:e+28] = _le16(3)  # start cluster
    s[e+28:e+32] = _le32(index_size)
    return s


def _build_file_sector(text):
    encoded = text.encode("ascii")
    s = list(encoded) + [0] * (SECTOR_SIZE - len(encoded))
    return s


class GhostFAT(wiring.Component):
    def __init__(self, block_count=64, *,
                 board_id="TinyFPGA-BX-v1", model="TinyFPGA BX",
                 url="https://tinyfpga.com"):
        self.block_count = block_count
        self.board_id = board_id
        self.model = model
        self.url = url
        self._build_sectors()

        super().__init__({
            "lba":   In(32),
            "start": In(1),
            "o":     Out(stream.Signature(_stream_layout)),
        })

    def _build_sectors(self):
        info_uf2_text = f"TinyFPGA Bootloader 1.0\r\nModel: {self.model}\r\nBoard-ID: {self.board_id}\r\n"
        index_htm_text = f'<!doctype html><html><body><script>location.replace("{self.url}");</script></body></html>\r\n'

        info_data = info_uf2_text.encode("ascii")
        index_data = index_htm_text.encode("ascii")
        build_time = datetime.now()

        sectors = {
            0: _build_boot_sector(self.block_count),
            FAT0_SECTOR: _build_fat_sector(),
            FAT1_SECTOR: _build_fat_sector(),
            ROOT_DIR_SECTOR: _build_root_dir(len(info_data), len(index_data), build_time),
            _data_sector(2): _build_file_sector(info_uf2_text),
            _data_sector(3): _build_file_sector(index_htm_text),
        }

        self._rom_data = []
        self._sector_map = []  # list of (lba, rom_base) tuples
        for lba in sorted(sectors.keys()):
            base = len(self._rom_data)
            self._rom_data.extend(sectors[lba])
            self._sector_map.append((lba, base))

    def elaborate(self, platform):
        m = Module()

        m.submodules.rom = rom = memory.Memory(
            shape=8, depth=len(self._rom_data), init=self._rom_data, attrs={"ram_style": "block"}
        )
        rom_rp = rom.read_port(domain="sync")

        rom_addr = Signal(range(len(self._rom_data) + 1))
        byte_count = Signal(range(SECTOR_SIZE))
        hit = Signal()

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += hit.eq(0)
                    # LBA lookup — check against known sector LBAs
                    for lba_const, rom_base in self._sector_map:
                        with m.If(self.lba == lba_const):
                            m.d.sync += [
                                rom_addr.eq(rom_base),
                                hit.eq(1),
                            ]
                    m.d.sync += byte_count.eq(0)
                    m.next = "CHECK_HIT"

            with m.State("CHECK_HIT"):
                with m.If(hit):
                    # Issue first read
                    m.d.comb += [
                        rom_rp.addr.eq(rom_addr),
                        rom_rp.en.eq(1),
                    ]
                    m.next = "STREAM"
                with m.Else():
                    m.next = "ZEROS"

            with m.State("STREAM"):
                m.d.comb += [
                    rom_rp.addr.eq(rom_addr + self.o.ready),
                    rom_rp.en.eq(1),

                    self.o.valid.eq(1),
                    self.o.p.data.eq(rom_rp.data),
                ]
                with m.If(self.o.ready):
                    m.d.sync += byte_count.eq(byte_count + 1)
                    with m.If(byte_count == (SECTOR_SIZE - 1)):
                        m.next = "IDLE"
                    with m.Else():
                        m.d.sync += rom_addr.eq(rom_addr + 1)


            with m.State("ZEROS"):
                m.d.comb += [
                    self.o.valid.eq(1),
                    self.o.p.data.eq(0),
                ]
                with m.If(self.o.ready):
                    m.d.sync += byte_count.eq(byte_count + 1)
                    with m.If(byte_count == (SECTOR_SIZE - 1)):
                        m.next = "IDLE"

        return m
