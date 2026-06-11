"""UF2 / Mass-Storage backend — the original drag-and-drop personality.

A single Mass-Storage (Bulk-Only Transport, SCSI transparent) interface whose
EP1 bulk endpoints feed `SCSIHandler` -> `UF2Decoder` -> `QspiFlash`. This is
the behaviour `Top` had before the backend refactor, moved here verbatim.
"""

from amaranth import ResetInserter, Signal, Mux
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import flipped

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint
from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash
from blocks.qspi import Mode
from blocks.qspi_ram import QspiRam
from blocks.dual_bank import DualBank
from blocks.boot_header import BootHeader
from tools.ecp_bitstream import flash_header

from . import Backend, Reconfig, Status

# tCEM watchdog threshold. 400 cycles is ~6.7 us at 60MHz. Below the APS6404L's 8 us max tCEM.
_RAM_TCEM_CYCLES = 400


class Uf2MscBackend(Backend):
    device_class = (0, 0, 0)
    personality = "UF2"

    def __init__(self, config, *, hs):
        super().__init__(config, hs=hs)

        self.usb_ids = (config.vid, config.pid)

        mps = 512 if hs else 64
        self.out_ep = USBStreamOutEndpoint(endpoint_number=1, max_packet_size=mps)
        self.in_ep  = USBStreamInEndpoint(endpoint_number=1, max_packet_size=mps)

        self.ms_handler = MassStorageRequestHandler(if_num=0)

        scsi_kwargs = dict(
            scsi_vendor=config.scsi_vendor,
            scsi_product=config.scsi_product,
            board_id=config.board_id,
            model=config.model,
            url=config.url,
        )
        self.scsi  = SCSIHandler(block_count=16 * 1024 * 1024 // 512,
                                 block_size=512, **scsi_kwargs)
        self.uf2   = UF2Decoder(base_addr=config.reload_image_offset)
        self.flash = QspiFlash()

        # Optional fast RAM bank (FLASH <-> QSPI PSRAM via cfg_ctrl).
        self.has_ram_bank = config.has_ram_bank
        if self.has_ram_bank:
            self.ram       = QspiRam()
            self.dual_bank = DualBank(max_cs_cycles=_RAM_TCEM_CYCLES)
            self.cfg_ctrl_o = self.dual_bank.cfg_ctrl_o
            self.boot_header = BootHeader(
                header_bytes=flash_header(),
                base_addr=config.reload_image_offset)

    def populate_configuration(self, c, *, bulk_mps):
        c.bMaxPower = 100

        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = 0
            i.bInterfaceClass    = 0x08  # Mass Storage
            i.bInterfaceSubclass = 0x06  # SCSI Transparent Command Set
            i.bInterfaceProtocol = 0x50  # Bulk-Only Transport
            i.iInterface = "UF2"

            with i.EndpointDescriptor() as ep:
                ep.bEndpointAddress = 0x01  # EP1 OUT
                ep.bmAttributes     = 0x02  # Bulk
                ep.wMaxPacketSize   = bulk_mps
                ep.bInterval        = 0

            with i.EndpointDescriptor() as ep:
                ep.bEndpointAddress = 0x81  # EP1 IN
                ep.bmAttributes     = 0x02  # Bulk
                ep.wMaxPacketSize   = bulk_mps
                ep.bInterval        = 0

    def request_handlers(self):
        return [self.ms_handler]

    def endpoints(self):
        return [self.out_ep, self.in_ep]

    def build(self, m, *, usb):
        # SCSI is reset on a USB bus reset (and a Mass-Storage class reset).
        m.submodules.scsi = scsi = ResetInserter(
            usb.reset_detected | self.ms_handler.reset)(self.scsi)
        m.submodules.uf2 = uf2 = self.uf2
        m.submodules.flash = flash = self.flash

        wiring.connect(m, ep_out=self.out_ep.o, scsi=scsi.rx)
        wiring.connect(m, ep_in=self.in_ep.i, scsi=scsi.tx)

        wiring.connect(m, scsi.write_stream, uf2.i)
        m.d.comb += [
            flash.done.eq(uf2.done),
            # Surface UF2 decode errors (e.g. bad end magic) to the host via
            # the SCSI CSW status byte.
            scsi.error_in.eq(uf2.error),
            # `clear` also fires on a USB bus reset so a mid-upload disconnect
            # doesn't leave uf2.done latched and accidentally re-arm Warmboot
            # once the bus comes back up.
            uf2.clear.eq(scsi.clear_decoder | usb.reset_detected),
        ]

        if not self.has_ram_bank:
            wiring.connect(m, uf2.o, flash.i)
            self.qo, self.qi = flash.qo, flash.qi
            arm = uf2.done
            activity = scsi.rx.valid | scsi.tx.valid | flash.qo.valid
        else:
            m.submodules.ram = ram = self.ram
            m.submodules.dual_bank = db = self.dual_bank
            m.submodules.boot_header = bh = self.boot_header

            # A UF2 tagged with the RAM familyID streams into the PSRAM.
            # Any other image is erased/programmed into FLASH. familyID is latched
            # from each header before block payload is emitted.
            ram_mode = Signal()
            m.d.comb += ram_mode.eq(uf2.familyID == self.config.ram_family_id)

            header_done = Signal()
            ensuring    = Signal()
            m.d.comb += ensuring.eq(ram_mode & ~header_done)

            with m.FSM(name="header"):
                with m.State("WAIT"):
                    with m.If(ensuring & ram.idle):
                        m.d.comb += bh.i_start.eq(1)
                        m.next = "RUN"
                with m.State("RUN"):
                    with m.If(bh.done):
                        m.d.sync += header_done.eq(1)
                        m.next = "WAIT"
            # Reset the latch only on a real new USB session — NOT on uf2.clear,
            # which pulses every SCSI command and would re-trigger mid-transfer.
            with m.If(usb.reset_detected):
                m.d.sync += header_done.eq(0)

            # Route the decoder's byte stream to the selected writer.
            for w in (flash, ram):
                m.d.comb += w.i.p.data.eq(uf2.o.p.data)
            m.d.comb += [
                flash.i.p.addr.eq(uf2.o.p.addr),
                ram.i.p.addr.eq(uf2.o.p.addr - self.uf2.base_addr),
                flash.i.valid.eq(uf2.o.valid & ~ram_mode),
                ram.i.valid.eq(uf2.o.valid & ram_mode & ~ensuring),
                ram.done.eq(uf2.done),
            ]
            with m.If(ensuring):
                m.d.comb += uf2.o.ready.eq(0)
            with m.Elif(ram_mode):
                m.d.comb += uf2.o.ready.eq(ram.i.ready)
            with m.Else():
                m.d.comb += uf2.o.ready.eq(flash.i.ready)

            # Bank-select + tCEM handshake.
            m.d.comb += [
                db.bank.eq(ram.bank),
                db.cs_open.eq(ram.cs_open),
                ram.tcem_expired.eq(db.tcem_expired),
            ]

            # Expose a single QSPI bus to Top, connected to the active writer:
            # BootHeader during the ensure, else the selected FLASH/RAM writer.
            self.qo = stream.Signature(data.StructLayout({
                "chip": range(2), "mode": Mode, "data": 8})).create()
            self.qi = stream.Signature(data.StructLayout({"data": 8})).flip().create()
            with m.If(ensuring):
                wiring.connect(m, flipped(self.qo), bh.qo)
                wiring.connect(m, flipped(self.qi), bh.qi)
            with m.Elif(ram_mode):
                wiring.connect(m, flipped(self.qo), ram.qo)
                wiring.connect(m, flipped(self.qi), ram.qi)
            with m.Else():
                wiring.connect(m, flipped(self.qo), flash.qo)
                wiring.connect(m, flipped(self.qi), flash.qi)

            # For a RAM upload, reconfigure waits for the writer's boot-arm
            arm = Mux(ram_mode, ram.boot_ready, uf2.done)
            activity = (scsi.rx.valid | scsi.tx.valid
                        | flash.qo.valid | ram.qo.valid | bh.qo.valid)

        # Status: ERROR (bad UF2) > DONE (decode, reload) > ACTIVE (USB traffic) > IDLE (default).
        with m.If(uf2.error):
            m.d.comb += self.status.eq(Status.ERROR)
        with m.Elif(uf2.done):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(activity):
            m.d.comb += self.status.eq(Status.ACTIVE)

        return Reconfig(arm=arm, activity=activity)
