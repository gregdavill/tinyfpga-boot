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
from blocks.flash import FlashPort

from . import Backend, Reconfig, Status


class Uf2MscBackend(Backend):
    device_class = (0, 0, 0)
    personality = "UF2"
    writes_flash = True

    def __init__(self, config, *, hs, alloc=None):
        super().__init__(config, hs=hs, alloc=alloc)

        self.usb_ids = (config.vid, config.pid)
        self._if_num = self.alloc.interface()
        self._ep_num = self.alloc.endpoint()

        mps = 512 if hs else 64
        self.out_ep = USBStreamOutEndpoint(endpoint_number=self._ep_num, max_packet_size=mps)
        self.in_ep  = USBStreamInEndpoint(endpoint_number=self._ep_num, max_packet_size=mps)

        self.ms_handler = MassStorageRequestHandler(if_num=self._if_num)

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

        self.has_ram_bank = config.has_ram_bank
        # Flash-write port + status fed back by bind_writers from the backing.
        self.wr      = FlashPort.create()
        self.writing = Signal()   # backing.active
        self.arm_in  = Signal()   # backing.arm

    def populate_configuration(self, c, *, bulk_mps):
        c.bMaxPower = 100

        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = self._if_num
            i.bInterfaceClass    = 0x08  # Mass Storage
            i.bInterfaceSubclass = 0x06  # SCSI Transparent Command Set
            i.bInterfaceProtocol = 0x50  # Bulk-Only Transport
            i.iInterface = "UF2"

            with i.EndpointDescriptor() as ep:
                ep.bEndpointAddress = self._ep_num         # bulk OUT
                ep.bmAttributes     = 0x02  # Bulk
                ep.wMaxPacketSize   = bulk_mps
                ep.bInterval        = 0

            with i.EndpointDescriptor() as ep:
                ep.bEndpointAddress = 0x80 | self._ep_num  # bulk IN
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

        wiring.connect(m, ep_out=self.out_ep.o, scsi=scsi.rx)
        wiring.connect(m, ep_in=self.in_ep.i, scsi=scsi.tx)

        wiring.connect(m, scsi.write_stream, uf2.i)
        m.d.comb += [
            # Surface UF2 decode errors (e.g. bad end magic) to the host via
            # the SCSI CSW status byte.
            scsi.error_in.eq(uf2.error),
            # `clear` also fires on a USB bus reset so a mid-upload disconnect
            # doesn't leave uf2.done latched and accidentally re-arm Warmboot
            # once the bus comes back up.
            uf2.clear.eq(scsi.clear_decoder | usb.reset_detected),
        ]

        # Drive the flash-write port
        ram_select = Signal()
        m.d.comb += ram_select.eq(
            (uf2.familyID == self.config.ram_family_id) if self.has_ram_bank else 0)
        m.d.comb += [
            self.wr.w.p.data.eq(uf2.o.p.data),
            self.wr.w.p.addr.eq(Mux(ram_select,
                                    uf2.o.p.addr - self.uf2.base_addr,
                                    uf2.o.p.addr)),
            self.wr.w.valid.eq(uf2.o.valid),
            uf2.o.ready.eq(self.wr.w.ready),
            self.wr.flush.eq(uf2.done),
            self.wr.ram_select.eq(ram_select),
        ]
        writing  = self.writing
        arm      = self.arm_in
        activity = scsi.rx.valid | scsi.tx.valid | writing

        # Status: ERROR (bad UF2) > DONE (decode, reload) > ACTIVE (image being written) > IDLE (default).
        with m.If(uf2.error):
            m.d.comb += self.status.eq(Status.ERROR)
        with m.Elif(uf2.done):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(writing):
            m.d.comb += self.status.eq(Status.ACTIVE)

        return Reconfig(arm=arm, activity=activity)
