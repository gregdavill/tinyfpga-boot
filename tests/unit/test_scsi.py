import unittest

from gateware.blocks.storage.scsi import (
    CBW_SIGNATURE, CSW_SIGNATURE, OP_INQUIRY, OP_MODE_SENSE_6, OP_READ_10,
    OP_READ_CAPACITY, OP_REQUEST_SENSE, OP_TEST_UNIT_READY, OP_WRITE_10,
    SCSIHandler, _le_bytes,
)
from tests.unit.util import stream_get, stream_put, simulate


def make_cbw(tag, transfer_length, flags, cb_bytes):
    cbw = [0] * 31
    for i, b in enumerate(_le_bytes(CBW_SIGNATURE, 4)):
        cbw[i] = b
    for i, b in enumerate(_le_bytes(tag, 4)):
        cbw[4 + i] = b
    for i, b in enumerate(_le_bytes(transfer_length, 4)):
        cbw[8 + i] = b
    cbw[12] = flags
    cbw[13] = 0
    cbw[14] = len(cb_bytes)
    for i, b in enumerate(cb_bytes[:16]):
        cbw[15 + i] = b
    return cbw


async def send_cbw(ctx, rx, cbw_bytes):
    for b in cbw_bytes:
        await stream_put(ctx, rx, {"data": b})


async def recv_data(ctx, tx, count):
    result = []
    for _ in range(count):
        p = await stream_get(ctx, tx)
        result.append(p["data"])
    return result


async def recv_csw(ctx, tx):
    data = await recv_data(ctx, tx, 13)
    sig = data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)
    assert sig == CSW_SIGNATURE, f"Bad CSW signature: 0x{sig:08X}"
    tag = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
    residue = data[8] | (data[9] << 8) | (data[10] << 16) | (data[11] << 24)
    status = data[12]
    return tag, residue, status


class TestSCSIHandler(unittest.TestCase):
    BLOCK_COUNT = 64
    BLOCK_SIZE = 512

    def _make_dut(self):
        return SCSIHandler(self.BLOCK_COUNT, self.BLOCK_SIZE)

    def test_inquiry(self):
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=1, transfer_length=36, flags=0x80, cb_bytes=[OP_INQUIRY])
            await send_cbw(ctx, dut.rx, cbw)

            data = await recv_data(ctx, dut.tx, 36)
            assert data[0] == 0x00, f"device type: {data[0]:#x}"
            assert data[1] == 0x80, f"removable: {data[1]:#x}"
            assert data[2] == 0x02, f"SCSI-2: {data[2]:#x}"
            vendor = bytes(data[8:16]).decode('ascii')
            assert vendor == "TINYFPGA", f"vendor: {vendor!r}"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 1
            assert residue == 0
            assert status == 0

        simulate(dut, testbench)

    def test_request_sense(self):
        """REQUEST_SENSE returns the 18-byte fixed sense buffer.
        """
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=8, transfer_length=18, flags=0x80, cb_bytes=[OP_REQUEST_SENSE])
            await send_cbw(ctx, dut.rx, cbw)

            data = await recv_data(ctx, dut.tx, 18)
            assert data[0] == 0x70, f"response code: {data[0]:#x} (want 0x70)"
            assert data[7] == 10,   f"additional sense length: {data[7]} (want 10)"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 8
            assert residue == 0
            assert status == 0

        simulate(dut, testbench)

    def test_mode_sense_6(self):
        """MODE_SENSE(6) returns the 4-byte mode parameter header
        (mode data length = 3).
        """
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=9, transfer_length=4, flags=0x80, cb_bytes=[OP_MODE_SENSE_6])
            await send_cbw(ctx, dut.rx, cbw)

            data = await recv_data(ctx, dut.tx, 4)
            assert data[0] == 3, f"mode data length: {data[0]} (want 3)"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 9
            assert residue == 0
            assert status == 0

        simulate(dut, testbench)

    def test_test_unit_ready(self):
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=2, transfer_length=0, flags=0x00, cb_bytes=[OP_TEST_UNIT_READY])
            await send_cbw(ctx, dut.rx, cbw)

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 2
            assert status == 0

        simulate(dut, testbench)

    def test_read_capacity(self):
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=3, transfer_length=8, flags=0x80, cb_bytes=[OP_READ_CAPACITY])
            await send_cbw(ctx, dut.rx, cbw)

            data = await recv_data(ctx, dut.tx, 8)
            last_lba = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
            assert last_lba == self.BLOCK_COUNT - 1, f"last_lba={last_lba}"
            bs = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
            assert bs == self.BLOCK_SIZE, f"block_size={bs}"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 3
            assert residue == 0
            assert status == 0

        simulate(dut, testbench)

    def test_read_boot_sector(self):
        dut = self._make_dut()

        async def testbench(ctx):
            # READ(10) for LBA 0: CDB byte 2-5 = big-endian LBA
            cb = [OP_READ_10, 0, 0, 0, 0, 0, 0, 1, 0, 0]  # LBA=0, transfer 1 block
            cbw = make_cbw(tag=4, transfer_length=512, flags=0x80, cb_bytes=cb)
            await send_cbw(ctx, dut.rx, cbw)

            data = await recv_data(ctx, dut.tx, 512)
            # Check boot sector signature
            assert data[510] == 0x55, f"sig lo: {data[510]:#x}"
            assert data[511] == 0xAA, f"sig hi: {data[511]:#x}"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 4
            assert residue == 0
            assert status == 0

        simulate(dut, testbench)

    def test_write_forwarded(self):
        dut = self._make_dut()
        received = []

        async def write_sink(ctx):
            ctx.set(dut.write_stream.ready, 1)
            for _ in range(512):
                payload, = await ctx.tick().sample(dut.write_stream.p.data).until(dut.write_stream.valid)
                received.append(payload)

        async def testbench(ctx):
            cbw = make_cbw(tag=5, transfer_length=512, flags=0x00, cb_bytes=[OP_WRITE_10])
            await send_cbw(ctx, dut.rx, cbw)

            for i in range(512):
                await stream_put(ctx, dut.rx, {"data": i & 0xFF})

            await ctx.tick().repeat(10)

            assert len(received) == 512
            for i in range(512):
                assert received[i] == (i & 0xFF), f"byte {i}: {received[i]} != {i & 0xFF}"

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 5
            assert residue == 0
            assert status == 0

        simulate(dut, testbench, write_sink)

    def test_unknown_command(self):
        dut = self._make_dut()

        async def testbench(ctx):
            cbw = make_cbw(tag=6, transfer_length=0, flags=0x00, cb_bytes=[0xFF])
            await send_cbw(ctx, dut.rx, cbw)

            tag, residue, status = await recv_csw(ctx, dut.tx)
            assert tag == 6
            assert status == 1, "unknown command should fail"

        simulate(dut, testbench)

    def test_invalid_cbw_signature_halts(self):
        """USB MSC §6.6.1: invalid CBW (bad signature) must NOT be
        dispatched. SCSIHandler should enter its HALT state without
        emitting a CSW — the host detects "no CSW" via timeout and
        runs Reset Recovery. Block-level test only confirms tx stays
        idle; integration-level recovery is covered by
        test_scsi_bad_cbw_does_not_deadlock in the cocotb suite,
        where the ResetInserter that clears HALT is also wired in."""
        dut = self._make_dut()

        async def testbench(ctx):
            # Build a valid CBW first, then corrupt the signature.
            cbw = make_cbw(tag=7, transfer_length=0, flags=0x00,
                           cb_bytes=[OP_TEST_UNIT_READY])
            cbw[0] = 0xDE   # smash dCBWSignature
            cbw[1] = 0xAD
            cbw[2] = 0xBE
            cbw[3] = 0xEF
            await send_cbw(ctx, dut.rx, cbw)

            # tx must stay idle — no CSW should be emitted. Give the
            # FSM 50 cycles to react; HALT enters within a few.
            ctx.set(dut.tx.ready, 1)
            for _ in range(50):
                await ctx.tick()
                assert not ctx.get(dut.tx.valid), \
                    "tx.valid asserted after bad CBW — HALT state leaked a CSW"

        simulate(dut, testbench)


if __name__ == "__main__":
    unittest.main()
