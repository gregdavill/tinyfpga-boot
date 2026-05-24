from amaranth import *
from amaranth.lib import wiring, stream, data, io
from blocks.ports import PortGroup

from usb_protocol.emitters   import DeviceDescriptorCollection
from luna.usb2               import USBDevice

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint
from config import SerialSource

from blocks.qspi import Controller
from blocks.serial_source import FlashUidSerialSource, SecurityPageSerialSource
from blocks.usb.serial_handler import USBStreamSerialDescriptorHandler

from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash


class Top(Elaboratable):
    def __init__(self, config=None):
        self.config = config


        # --- Serial-number source (selected by config) ---
        kind = config.serial_source if config else SerialSource.FLASH_UID
        self.serial_source = {
            SerialSource.SECURITY_PAGE: SecurityPageSerialSource,
            SerialSource.FLASH_UID:     FlashUidSerialSource,
        }[kind]()

        # --- Control-endpoint request handler ---
        self.ms_handler = MassStorageRequestHandler(if_num=0)

        # --- Mass-storage / UF2 write path ---
        scsi_kwargs = {}
        if config:
            scsi_kwargs = dict(
                scsi_vendor=config.scsi_vendor,
                scsi_product=config.scsi_product,
                board_id=config.board_id,
                model=config.model,
                url=config.url,
            )
        self.scsi = SCSIHandler(block_count=16 * 1024 * 1024 // 512,
                                block_size=512, **scsi_kwargs)
        self.uf2 = UF2Decoder(
            base_addr=config.reload_image_offset if config else 0)
        self.flash = QspiFlash()

    def create_descriptors(self, hs):
        """ Create the descriptors we want to use for our device. """

        # High-speed bulk endpoints must advertise 512-byte max packets.
        bulk_mps = 512 if hs else 64

        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = self.config.vid if self.config else 0x1209
            d.idProduct          = self.config.pid if self.config else 0x5af0

            d.bcdDevice          = 2.0

            d.iManufacturer      = self.config.manufacturer if self.config else "TinyFPGA"
            d.iProduct           = self.config.product if self.config else "Bootloader"
            d.iSerialNumber      = ""

            d.bNumConfigurations = 1

        # Store string descriptor index
        self.descriptor_iSerialNumber = d.fields['iSerialNumber']

        with descriptors.ConfigurationDescriptor() as c:

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

        return descriptors
    

    def elaborate(self, platform):
        m = Module()

        # Clock/reset generation is platform-intrinsic
        platform.create_clocks(m)

        usb_bus = platform.request(platform.default_usb_connection)
        m.submodules.usb = usb = DomainRenamer({'usb':'sync'})(USBDevice(bus=usb_bus))

        # USB descriptors, endpoints and the serial handler — their bulk
        # packet size (512 HS / 64 FS) depends on platform.is_hs.
        descriptors = self.create_descriptors(hs=platform.is_hs)
        mps = 512 if platform.is_hs else 64
        out_ep = USBStreamOutEndpoint(endpoint_number=1, max_packet_size=mps)
        in_ep  = USBStreamInEndpoint(endpoint_number=1, max_packet_size=mps)
        serial_handler = USBStreamSerialDescriptorHandler(
            self.descriptor_iSerialNumber, max_len=self.serial_source.max_len)

        # QSPI flash; the clock routing is platform-specific.
        if platform.flash_clk == "usrmclk":
            # ECP5 only: the flash clock is the dedicated config MCLK,
            # reachable only via the USRMCLK primitive.
            flash_io = platform.request('spi_flash_4x', dir='-')
            flash_clk = io.SimulationPort("o", 1)
            qspi_ports = PortGroup(cs=flash_io.cs, clk=flash_clk, dq=flash_io.dq)
            m.submodules.qspi = qspi = Controller(qspi_ports, chip_count=1, offset=0)
            m.submodules.usrmclk = Instance(
                "USRMCLK",
                i_USRMCLKI=flash_clk.o,      # fabric SPI clock -> config flash CCLK
                i_USRMCLKTS=Const(0),        # 0 = drive the clock (not tri-stated)
            )
        else:
            # Ordinary I/O pad for the flash clock (full DDR rate). The flash
            # resource must include a `clk` subsignal the controller drives.
            m.submodules.qspi = qspi = Controller(platform.request('spi_flash_4x', dir='-'), chip_count=1, offset=0)

        m.submodules.serial_source = ss = self.serial_source

        # Add our standard control endpoint to the device.
        ep = usb.add_standard_control_endpoint(
            descriptors, skiplist=[serial_handler.handler_condition])
        ep.add_request_handler(serial_handler)
        ep.add_request_handler(self.ms_handler)
        m.d.comb += [
            qspi.divisor.eq(4),
            # Feed the serial source's ASCII byte stream into the handler.
            serial_handler.serial_data.eq(ss.data.p.data),
            serial_handler.serial_valid.eq(ss.data.valid),
            ss.data.ready.eq(serial_handler.serial_ready),
        ]

        # Bulk data endpoints.
        usb.add_endpoint(out_ep)
        usb.add_endpoint(in_ep)

        # Mass-storage / UF2 write path. SCSI is reset on a USB bus reset
        # (and a Mass-Storage class reset).
        m.submodules.scsi = scsi = ResetInserter(usb.reset_detected | self.ms_handler.reset)(self.scsi)
        m.submodules.uf2 = uf2 = self.uf2
        m.submodules.flash = flash = self.flash

        # System-reconfigure trigger the platform owns the primitive. 
        # A bus reset cancels a pending reload, and `activity` keeps 
        # it from firing mid-transaction.
        reload_slot = self.config.reload_slot if self.config else 1
        reload_idle = (
            self.config.reload_idle_cycles
            if (self.config and self.config.reload_idle_cycles is not None)
            else 600_000
        )
        platform.create_reconfigure(
            m,
            arm=uf2.done,
            activity=scsi.rx.valid | scsi.tx.valid | flash.qo.valid,
            reset=usb.reset_detected,
            slot=reload_slot,
            idle_cycles=reload_idle,
        )

        wiring.connect(m, ep_out=out_ep.o, scsi=scsi.rx)
        wiring.connect(m, ep_in=in_ep.i, scsi=scsi.tx)

        with m.FSM():
            with m.State('BOOT-READ'):
                # Read the serial source over QSPI before USB comes up,
                # then hand the bus over to the flash write path.
                wiring.connect(m, ss.o, qspi.i)
                wiring.connect(m, ss.i, qspi.o)
                m.d.comb += ss.req.eq(1)
                with m.If(ss.done):
                    m.next = 'USB-CONNECT'

            with m.State('USB-CONNECT'):
                m.d.comb += usb.connect.eq(1)

                wiring.connect(m, scsi.write_stream, uf2.i)
                wiring.connect(m, uf2.o, flash.i)
                wiring.connect(m, flash.qo, qspi.i)
                wiring.connect(m, flash.qi, qspi.o)
                m.d.comb += [
                    flash.done.eq(uf2.done),
                    
                    # Surface UF2 decode errors (e.g. bad end magic)
                    # to the host via the SCSI CSW status byte.
                    scsi.error_in.eq(uf2.error),
                    # `clear` also fires on a USB bus reset so a
                    # mid-upload disconnect doesn't leave uf2.done
                    # latched and accidentally re-arm Warmboot once
                    # the bus comes back up.
                    uf2.clear.eq(scsi.clear_decoder | usb.reset_detected),
                ]

        return m
