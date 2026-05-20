from amaranth import *
from amaranth.lib import data, wiring, stream, memory
from amaranth.lib.wiring import In, Out

from .ghostfat import GhostFAT, SECTOR_SIZE

from usb_protocol.types import USBRequestType, USBRequestRecipient
from luna.gateware.usb.usb2.request import USBRequestHandler
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.stream.generator import StreamSerializer


# Mass Storage class requests
MS_REQUEST_RESET       = 0xFF
MS_REQUEST_GET_MAX_LUN = 0xFE


class MassStorageRequestHandler(USBRequestHandler):
    """Handles Mass Storage Bulk-Only Transport class requests (Get Max LUN, Reset)."""

    def __init__(self, if_num):
        super().__init__()
        self.if_num = if_num
        self.request_done = Signal()
        self.reset = Signal()

    def elaborate(self, platform):
        m = Module()

        m.submodules.transmitter = transmitter = StreamSerializer(
            data_length=1, domain='usb', stream_type=USBInStreamInterface
        )

        interface = self.interface
        setup = interface.setup

        targeting_if = (
            (setup.type == USBRequestType.CLASS) &
            (setup.recipient == USBRequestRecipient.INTERFACE) &
            (setup.index == self.if_num)
        )

        m.d.usb += self.request_done.eq(0)

        with m.FSM(domain='usb'):
            with m.State('IDLE'):
                with m.If(setup.received & targeting_if):
                    with m.Switch(setup.request):
                        with m.Case(MS_REQUEST_GET_MAX_LUN):
                            m.next = 'GET_MAX_LUN'
                        with m.Case(MS_REQUEST_RESET):
                            m.next = 'RESET'

            with m.State('GET_MAX_LUN'):
                m.d.comb += interface.claim.eq(1)

                # Send single byte: max LUN = 0
                m.d.comb += [
                    transmitter.stream.attach(interface.tx),
                    Cat(transmitter.data).eq(0),
                    transmitter.start.eq(interface.data_requested),
                ]

                with m.If(interface.status_requested):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                    m.d.usb += self.request_done.eq(1)

                with m.If(self.request_done):
                    m.next = 'IDLE'

            with m.State('RESET'):
                m.d.comb += interface.claim.eq(1)

                # Respond with ZLP
                with m.If(interface.status_requested):
                    m.d.comb += self.send_zlp()
                    m.d.usb += self.request_done.eq(1)

                with m.If(self.request_done):
                    m.d.comb += self.reset.eq(1)
                    m.next = 'IDLE'

        return m


CBW_SIGNATURE = 0x43425355
CSW_SIGNATURE = 0x53425355

# SCSI opcodes
OP_TEST_UNIT_READY = 0x00
OP_REQUEST_SENSE   = 0x03
OP_INQUIRY         = 0x12
OP_MODE_SENSE_6    = 0x1A
OP_READ_CAPACITY   = 0x25
OP_READ_10         = 0x28
OP_WRITE_10        = 0x2A

_stream_layout = data.StructLayout({"data": 8, "first": 1, "last": 1})


def _le_bytes(value, n):
    return [(value >> (8 * i)) & 0xFF for i in range(n)]


def _be_bytes(value, n):
    return [(value >> (8 * (n - 1 - i))) & 0xFF for i in range(n)]


def _pad_string(s, length):
    return list((s.ljust(length)[:length]).encode('ascii'))


class SCSIHandler(wiring.Component):
    def __init__(self, block_count, block_size=512, *,
                 scsi_vendor="TINYFPGA", scsi_product="UF2 Bootloader",
                 board_id="TinyFPGA-BX-v1", model="TinyFPGA BX",
                 url="https://tinyfpga.com"):
        self.block_count = block_count
        self.block_size = block_size
        self.scsi_vendor = scsi_vendor
        self.scsi_product = scsi_product
        self.board_id = board_id
        self.model = model
        self.url = url
        self._rom_data, self._rom_map = self._build_rom()

        super().__init__({
            "rx":           In(stream.Signature(_stream_layout)),
            "tx":           Out(stream.Signature(_stream_layout)),
            "tx_flush":     Out(1),
            "write_stream": Out(stream.Signature(_stream_layout)),
            # Downstream error feedback
            "error_in":      In(1),
            # 1-cycle pulse asserted in DISPATCH so the downstream UF2
            # decoder can clear its transfer-level state
            "clear_decoder": Out(1),
        })

    def _build_rom(self):
        rom = []
        rom_map = {}

        # INQUIRY (36 bytes)
        base = len(rom)
        inquiry = [0] * 36
        inquiry[0] = 0x00   # direct access block device
        inquiry[1] = 0x80   # removable
        inquiry[2] = 0x02   # SCSI-2
        inquiry[3] = 0x02   # response data format
        inquiry[4] = 31     # additional length
        inquiry[8:16]  = _pad_string(self.scsi_vendor, 8)
        inquiry[16:32] = _pad_string(self.scsi_product, 16)
        inquiry[32:36] = _pad_string("1.0", 4)
        rom.extend(inquiry)
        rom_map[OP_INQUIRY] = (base, 36)

        # REQUEST_SENSE (18 bytes)
        base = len(rom)
        sense = [0] * 18
        sense[0] = 0x70
        sense[7] = 10
        rom.extend(sense)
        rom_map[OP_REQUEST_SENSE] = (base, 18)

        # MODE_SENSE(6) (4 bytes)
        base = len(rom)
        mode = [0] * 4
        mode[0] = 3
        rom.extend(mode)
        rom_map[OP_MODE_SENSE_6] = (base, 4)

        # READ_CAPACITY(10) (8 bytes) - big-endian
        base = len(rom)
        cap = _be_bytes(self.block_count - 1, 4) + _be_bytes(self.block_size, 4)
        rom.extend(cap)
        rom_map[OP_READ_CAPACITY] = (base, 8)

        return rom, rom_map

    def elaborate(self, platform):
        m = Module()

        m.submodules.rom = rom = memory.Memory(shape=8, depth=len(self._rom_data), init=self._rom_data)
        rom_rp = rom.read_port(domain="sync")

        # GhostFAT submodule
        m.submodules.ghostfat = ghostfat = GhostFAT(
            block_count=self.block_count,
            board_id=self.board_id, model=self.model, url=self.url,
        )

        # CBW fields
        cbw_count = Signal(range(31))
        cbw_signature = Signal(32)
        cbw_tag = Signal(32)
        transfer_length = Signal(32)
        cbw_flags = Signal(8)
        opcode = Signal(8)
        scsi_lba = Signal(32)

        # Response sending
        rom_offset = Signal(range(max(len(self._rom_data), 1) + 1))
        rom_end = Signal(range(max(len(self._rom_data), 1) + 1))
        data_sent = Signal(32)

        # CSW
        csw_count = Signal(range(13))
        csw_status = Signal()
        csw_data = Signal(13 * 8)

        rx = self.rx
        tx = self.tx

        # Flush pulse
        tx_flush_r = Signal()
        m.d.sync += self.tx_flush.eq(tx_flush_r)
        m.d.sync += tx_flush_r.eq(0)

        with m.FSM():
            # ---- RECEIVE CBW (31 bytes) ----
            with m.State("RECEIVE_CBW"):
                m.d.comb += rx.ready.eq(1)
                with m.If(rx.valid):
                    # Latch individual fields by byte position
                    # Signature: bytes 0-3 (LE). USB MSC §6.6.1
                    # requires the device to detect an invalid CBW
                    # signature and STALL / require Reset Recovery.
                    for i in range(4):
                        with m.If(cbw_count == i):
                            m.d.sync += cbw_signature[i*8:(i+1)*8].eq(rx.p.data)
                    # Tag: bytes 4-7 (LE)
                    for i in range(4):
                        with m.If(cbw_count == (4 + i)):
                            m.d.sync += cbw_tag[i*8:(i+1)*8].eq(rx.p.data)
                    # Transfer length: bytes 8-11 (LE)
                    for i in range(4):
                        with m.If(cbw_count == (8 + i)):
                            m.d.sync += transfer_length[i*8:(i+1)*8].eq(rx.p.data)
                    # Flags: byte 12
                    with m.If(cbw_count == 12):
                        m.d.sync += cbw_flags.eq(rx.p.data)
                    # Opcode: byte 15 (first byte of CBWCB)
                    with m.If(cbw_count == 15):
                        m.d.sync += opcode.eq(rx.p.data)
                    # LBA: bytes 17-20 (CDB bytes 2-5, big-endian)
                    for i in range(4):
                        with m.If(cbw_count == (17 + i)):
                            m.d.sync += scsi_lba[(3-i)*8:(4-i)*8].eq(rx.p.data)

                    with m.If(cbw_count == 30):
                        m.d.sync += cbw_count.eq(0)
                        with m.If(cbw_signature == C(CBW_SIGNATURE, 32)):
                            m.next = "DISPATCH"
                        with m.Else():
                            # Invalid CBW — USB MSC §6.6.1. Wait for
                            # Reset Recovery no CSW for this bad CBW
                            m.next = "HALT"
                    with m.Else():
                        m.d.sync += cbw_count.eq(cbw_count + 1)

            # ---- HALT: parked here after an invalid CBW. Await module level reset
            with m.State("HALT"):
                pass

            # ---- DISPATCH on SCSI opcode ----
            with m.State("DISPATCH"):
                # Reset transfer-level decoder state
                m.d.comb += self.clear_decoder.eq(1)
                m.d.sync += [
                    data_sent.eq(0),
                    csw_status.eq(0),
                ]
                with m.Switch(opcode):
                    with m.Case(OP_TEST_UNIT_READY):
                        m.next = "SEND_CSW_PREP"

                    for op, (base, length) in self._rom_map.items():
                        with m.Case(op):
                            m.d.sync += [
                                rom_offset.eq(base),
                                rom_end.eq(base + length),
                            ]
                            # Prime the ROM read port so rom[base] is
                            # valid on the first SEND_RESPONSE cycle.
                            m.d.comb += [
                                rom_rp.addr.eq(base),
                                rom_rp.en.eq(1),
                            ]
                            m.next = "SEND_RESPONSE"

                    with m.Case(OP_READ_10):
                        # USB MSC + SBC-3: an LBA past the reported
                        # capacity must fail. Route through a
                        # one-byte short-packet state that drains the
                        # data phase via a single dummy byte then
                        # send CSW status=1.
                        with m.If(scsi_lba >= self.block_count):
                            m.d.sync += csw_status.eq(1)
                            m.next = "SEND_SHORT_DATA"
                        with m.Else():
                            m.d.sync += ghostfat.start.eq(1)
                            m.d.comb += ghostfat.lba.eq(scsi_lba)
                            m.next = "SEND_SECTOR"

                    with m.Case(OP_WRITE_10):
                        with m.If(scsi_lba >= self.block_count):
                            m.d.sync += csw_status.eq(1)
                        m.next = "RECEIVE_WRITE_DATA"

                    with m.Default():
                        m.d.sync += csw_status.eq(1)
                        m.next = "SEND_CSW_PREP"

            # ---- SEND_RESPONSE: ----
            with m.State("SEND_RESPONSE"):
                m.d.comb += [
                    rom_rp.addr.eq(rom_offset + tx.ready),
                    rom_rp.en.eq(1),
                ]

                m.d.comb += [
                    tx.valid.eq(1),
                    tx.p.data.eq(rom_rp.data),
                    tx.p.first.eq(data_sent == 0),
                    tx.p.last.eq(data_sent == (transfer_length - 1)),
                ]
                with m.If(tx.ready):
                    m.d.sync += [
                        data_sent.eq(data_sent + 1),
                        rom_offset.eq(rom_offset + 1),
                    ]
                    with m.If(data_sent == (transfer_length - 1)):
                        m.next = "SEND_CSW_PREP"
                    with m.Elif(rom_offset == (rom_end - 1)):
                        m.next = "SEND_ZEROS_CONT"

            # ---- SEND_ZEROS_CONT: ----
            with m.State("SEND_ZEROS_CONT"):
                m.d.comb += [
                    tx.valid.eq(1),
                    tx.p.data.eq(0),
                    tx.p.first.eq(0),
                    # tx.p.last.eq(data_sent == (transfer_length - 1)),
                ]
                with m.If(tx.ready):
                    m.d.sync += data_sent.eq(data_sent + 1)
                    with m.If(data_sent == (transfer_length - 1)):
                        m.next = "SEND_CSW_PREP"

            # ---- SEND_SECTOR: stream from GhostFAT ----
            with m.State("SEND_SECTOR"):
                sector_byte = Signal(range(SECTOR_SIZE), name="sector_byte")

                m.d.sync += ghostfat.start.eq(0)
                m.d.comb += ghostfat.lba.eq(scsi_lba)

                m.d.comb += [
                    tx.valid.eq(ghostfat.o.valid),
                    tx.p.data.eq(ghostfat.o.p.data),
                    tx.p.first.eq(data_sent == 0),
                    # tx.p.last.eq(data_sent == (transfer_length - 1)),
                    ghostfat.o.ready.eq(tx.ready),
                ]
                with m.If(tx.ready & ghostfat.o.valid):
                    m.d.sync += [
                        data_sent.eq(data_sent + 1),
                        sector_byte.eq(sector_byte + 1),
                    ]
                    with m.If(data_sent == (transfer_length - 1)):
                        m.d.sync += sector_byte.eq(0)
                        m.next = "SEND_CSW_PREP"
                    with m.Elif(sector_byte == (SECTOR_SIZE - 1)):
                        # Multi-sector: advance to next LBA
                        m.d.sync += [
                            scsi_lba.eq(scsi_lba + 1),
                            ghostfat.start.eq(1),
                            sector_byte.eq(0),
                        ]

            # ---- SEND_SHORT_DATA: emit a single zero byte as a
            # short data packet to terminate the IN data phase.
            # 
            # This could be a ZLP, but USBStreamInEndpoint's 
            # in LUNA don't support sending a stray ZLP without
            # any preceeding data.
            with m.State("SEND_SHORT_DATA"):
                m.d.comb += [
                    tx.valid.eq(1),
                    tx.p.data.eq(0),
                    tx.p.first.eq(1),
                    tx.p.last.eq(1),
                ]
                with m.If(tx.ready):
                    m.d.sync += data_sent.eq(1)
                    m.next = "SEND_CSW_PREP"

            # ---- RECEIVE_WRITE_DATA ----
            with m.State("RECEIVE_WRITE_DATA"):
                with m.If(csw_status):
                    m.d.comb += rx.ready.eq(1)
                with m.Else():
                    m.d.comb += [
                        self.write_stream.valid.eq(rx.valid),
                        self.write_stream.p.data.eq(rx.p.data),
                        self.write_stream.p.first.eq(data_sent == 0),
                        self.write_stream.p.last.eq(data_sent == (transfer_length - 1)),
                        rx.ready.eq(self.write_stream.ready),
                    ]
                with m.If(rx.valid & rx.ready):
                    m.d.sync += data_sent.eq(data_sent + 1)
                    with m.If(data_sent == (transfer_length - 1)):
                        m.next = "SEND_CSW_PREP"

            # ---- SEND_CSW_PREP ----
            with m.State("SEND_CSW_PREP"):
                residue = Signal(32, name="residue")
                m.d.comb += residue.eq(transfer_length - data_sent)
                # OR decoder-level error sampled this cycle.
                final_status = Signal(name="final_status")
                m.d.comb += final_status.eq(csw_status | self.error_in)
                m.d.sync += [
                    csw_data.eq(Cat(
                        C(CSW_SIGNATURE, 32),
                        cbw_tag,
                        residue,
                        final_status,
                        C(0, 7),  # pad status to fill byte 12
                    )),
                    csw_count.eq(0),
                ]
                m.next = "SEND_CSW"

            # ---- SEND_CSW: shift out 13 bytes ----
            with m.State("SEND_CSW"):
                m.d.comb += [
                    tx.valid.eq(1),
                    tx.p.data.eq(csw_data[:8]),
                    tx.p.first.eq(csw_count == 0),
                    tx.p.last.eq(csw_count == 12),
                ]
                with m.If(tx.ready):
                    m.d.sync += [
                        csw_data.eq(csw_data >> 8),
                        csw_count.eq(csw_count + 1),
                    ]
                    with m.If(csw_count == 12):
                        m.d.sync += tx_flush_r.eq(1)
                        m.next = "RECEIVE_CBW"

        return m


# ---------- Tests ----------
import unittest
from .test_util import stream_get, stream_put, simulate


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
