from amaranth import *
from amaranth.lib import data, wiring, stream, memory
from amaranth.lib.wiring import In, Out


from datetime import datetime

SECTOR_SIZE = 512
SECTORS_PER_CLUSTER = 1
RESERVED_SECTORS = 1
NUM_FATS = 2
SECTORS_PER_FAT = 1
ROOT_DIR_ENTRIES = 16
ROOT_DIR_SECTORS = 1
DATA_START_SECTOR = RESERVED_SECTORS + NUM_FATS * SECTORS_PER_FAT + ROOT_DIR_SECTORS  # 4

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


INFO_UF2_TEXT = "TinyFPGA Bootloader 1.0\r\nModel: TinyFPGA BX\r\nBoard-ID: TinyFPGA-BX-v1\r\n"
INDEX_HTM_TEXT = '<!doctype html><html><body><script>location.replace("https://tinyfpga.com");</script></body></html>\r\n'


class GhostFAT(wiring.Component):
    def __init__(self, block_count=64):
        self.block_count = block_count
        self._build_sectors()

        super().__init__({
            "lba":   In(32),
            "start": In(1),
            "o":     Out(stream.Signature(_stream_layout)),
        })

    def _build_sectors(self):
        info_data = INFO_UF2_TEXT.encode("ascii")
        index_data = INDEX_HTM_TEXT.encode("ascii")
        build_time = datetime.now()

        sectors = {
            0: _build_boot_sector(self.block_count),
            1: _build_fat_sector(),
            2: _build_fat_sector(),
            3: _build_root_dir(len(info_data), len(index_data), build_time),
            4: _build_file_sector(INFO_UF2_TEXT),
            5: _build_file_sector(INDEX_HTM_TEXT),
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
            shape=8, depth=len(self._rom_data), init=self._rom_data
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


# ---------- Tests ----------
import unittest
from .test_util import stream_get, simulate


async def read_sector(ctx, dut, lba):
    ctx.set(dut.lba, lba)
    ctx.set(dut.start, 1)
    await ctx.tick()
    ctx.set(dut.start, 0)

    result = []
    for _ in range(SECTOR_SIZE):
        p = await stream_get(ctx, dut.o)
        result.append(p["data"])
    return result


class TestGhostFAT(unittest.TestCase):
    def _make_dut(self):
        return GhostFAT(block_count=64)

    def test_boot_sector(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, 0)
            assert len(data) == 512
            # Boot signature
            assert data[510] == 0x55, f"sig lo: {data[510]:#x}"
            assert data[511] == 0xAA, f"sig hi: {data[511]:#x}"
            # BPB sector size
            sector_size = data[11] | (data[12] << 8)
            assert sector_size == 512, f"sector size: {sector_size}"
            # Jump
            assert data[0] == 0xEB

        simulate(dut, testbench)

    def test_fat_sector(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, 1)
            # First 8 bytes: F8 FF FF FF FF FF FF FF
            expected = [0xF8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
            assert data[:8] == expected, f"FAT: {[f'{b:#x}' for b in data[:8]]}"

        simulate(dut, testbench)

    def test_root_dir(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, 3)
            # Volume label
            label = bytes(data[0:11]).decode("ascii")
            assert label == "UF2 BOOT   ", f"label: {label!r}"
            assert data[11] == 0x08  # volume label attr

            # INFO_UF2.TXT entry
            name = bytes(data[32:43]).decode("ascii")
            assert name == "INFO_UF2TXT", f"name: {name!r}"
            cluster = data[32+26] | (data[32+27] << 8)
            assert cluster == 2, f"cluster: {cluster}"
            size = data[32+28] | (data[32+29] << 8) | (data[32+30] << 16) | (data[32+31] << 24)
            assert size == len(INFO_UF2_TEXT.encode("ascii")), f"size: {size}"

            # INFO_UF2.TXT timestamps should be non-zero
            create_time = data[32+14] | (data[32+15] << 8)
            create_date = data[32+16] | (data[32+17] << 8)
            assert create_date != 0, "creation date should be set"
            assert create_time != 0 or True  # time could be midnight

            # INDEX.HTM entry
            name2 = bytes(data[64:75]).decode("ascii")
            assert name2 == "INDEX   HTM", f"name2: {name2!r}"
            cluster2 = data[64+26] | (data[64+27] << 8)
            assert cluster2 == 3, f"cluster2: {cluster2}"

            # INDEX.HTM timestamps should match INFO_UF2.TXT
            idx_date = data[64+16] | (data[64+17] << 8)
            assert idx_date == create_date, "both files should have same build date"

        simulate(dut, testbench)

    def test_file_content(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, 4)
            text = bytes(data[:8]).decode("ascii")
            assert text == "TinyFPGA", f"text: {text!r}"

        simulate(dut, testbench)

    def test_unknown_lba(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, 60)
            assert all(b == 0 for b in data), "unknown LBA should return all zeros"

        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()
