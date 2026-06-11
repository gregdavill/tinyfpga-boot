"""UF2 / Mass-Storage backend — the original drag-and-drop personality.

A single Mass-Storage (Bulk-Only Transport, SCSI transparent) interface whose
EP1 bulk endpoints feed `SCSIHandler` -> `UF2Decoder` -> `QspiFlash`. This is
the behaviour `Top` had before the backend refactor, moved here verbatim.
"""

from amaranth import ResetInserter, Signal, Mux
from amaranth.lib import wiring

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint
from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash
from blocks.dual_bank_writer import DualBankWriter
from tools.ecp_bitstream import flash_header

from . import Backend, Reconfig, Status


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

        self.has_ram_bank = config.has_ram_bank
        if self.has_ram_bank:
            self.writer = DualBankWriter(
                header_bytes=flash_header(),
                base_addr=config.reload_image_offset)
            self.cfg_ctrl_o = self.writer.cfg_ctrl_o

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
            m.submodules.writer = writer = self.writer

            # A UF2 tagged with the RAM familyID streams into the PSRAM; any
            # other image is erased/programmed into FLASH. familyID is latched
            # from each block header before its payload is emitted.
            ram_select = Signal()
            m.d.comb += ram_select.eq(uf2.familyID == self.config.ram_family_id)

            # FLASH keeps the decoder's slot-1 relocation; the RAM image is a
            # standalone bitstream based at 0
            m.d.comb += [
                writer.i.p.data.eq(uf2.o.p.data),
                writer.i.p.addr.eq(Mux(ram_select,
                                       uf2.o.p.addr - self.uf2.base_addr,
                                       uf2.o.p.addr)),
                writer.i.valid.eq(uf2.o.valid),
                uf2.o.ready.eq(writer.i.ready),
                writer.ram_select.eq(ram_select),
                writer.done.eq(uf2.done),
                writer.clear.eq(usb.reset_detected),
            ]
            self.qo, self.qi = writer.qo, writer.qi
            arm = writer.arm
            activity = scsi.rx.valid | scsi.tx.valid | writer.active

        # Status: ERROR (bad UF2) > DONE (decode, reload) > ACTIVE (USB traffic) > IDLE (default).
        with m.If(uf2.error):
            m.d.comb += self.status.eq(Status.ERROR)
        with m.Elif(uf2.done):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(activity):
            m.d.comb += self.status.eq(Status.ACTIVE)

        return Reconfig(arm=arm, activity=activity)
