"""UF2 / Mass-Storage backend — the original drag-and-drop personality.

A single Mass-Storage (Bulk-Only Transport, SCSI transparent) interface whose
EP1 bulk endpoints feed `SCSIHandler` -> `UF2Decoder` -> `QspiFlash`. This is
the behaviour `Top` had before the backend refactor, moved here verbatim.
"""

from amaranth import ResetInserter
from amaranth.lib import wiring

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint
from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash

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
        wiring.connect(m, uf2.o, flash.i)
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

        # Status: ERROR (bad UF2) > DONE (decode, reload) > ACTIVE (USB traffic) > IDLE (default).
        with m.If(uf2.error):
            m.d.comb += self.status.eq(Status.ERROR)
        with m.Elif(uf2.done):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(scsi.rx.valid | scsi.tx.valid | flash.qo.valid):
            m.d.comb += self.status.eq(Status.ACTIVE)

        # QSPI bus: Top muxes these against the serial source inside USB-CONNECT.
        self.qo = flash.qo
        self.qi = flash.qi

        return Reconfig(
            arm=uf2.done,
            activity=scsi.rx.valid | scsi.tx.valid | flash.qo.valid,
        )
