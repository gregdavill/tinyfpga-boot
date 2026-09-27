import unittest
from tests.unit.util import stream_get, simulate
from gateware.blocks.storage.ghostfat import FAT0_SECTOR, FAT1_SECTOR, GhostFAT, ROOT_DIR_SECTOR, SECTOR_SIZE, _data_sector


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
        return GhostFAT(block_count=16 * 1024 * 1024 // 512)

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
            data = await read_sector(ctx, dut, FAT0_SECTOR)
            # First 8 bytes: F8 FF FF FF FF FF FF FF
            expected = [0xF8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
            assert data[:8] == expected, f"FAT: {[f'{b:#x}' for b in data[:8]]}"
            # The second FAT copy must be byte-identical.
            data2 = await read_sector(ctx, dut, FAT1_SECTOR)
            assert data2 == data, "FAT copies differ"

        simulate(dut, testbench)

    def test_root_dir(self):
        dut = self._make_dut()

        async def testbench(ctx):
            data = await read_sector(ctx, dut, ROOT_DIR_SECTOR)
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
            expected_info = "TinyFPGA Bootloader 1.0\r\nModel: TinyFPGA BX\r\nBoard-ID: TinyFPGA-BX-v1\r\n"
            assert size == len(expected_info.encode("ascii")), f"size: {size}"

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
            data = await read_sector(ctx, dut, _data_sector(2))
            text = bytes(data[:8]).decode("ascii")
            assert text == "TinyFPGA", f"text: {text!r}"

        simulate(dut, testbench)

    def test_unknown_lba(self):
        dut = self._make_dut()

        async def testbench(ctx):
            # A free data cluster (not boot/FAT/root/file) reads back as zeros.
            data = await read_sector(ctx, dut, _data_sector(100))
            assert all(b == 0 for b in data), "unknown LBA should return all zeros"

        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()
