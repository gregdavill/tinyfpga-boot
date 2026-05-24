from amaranth import *
from amaranth.lib import wiring, stream, data

from usb_protocol.emitters   import DeviceDescriptorCollection
from luna.usb2               import USBDevice

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint

from blocks.qspi import Controller
from blocks.serial_source import FlashUidSerialSource, SecurityPageSerialSource
from blocks.usb.serial_handler import USBStreamSerialDescriptorHandler
from blocks.dfu import DFUHandler

from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash
from blocks.warmboot import Warmboot


class Top(Elaboratable):
    def __init__(self, config=None):
        self.config = config

        # Build the descriptor set up front; this also stashes
        # `self.descriptor_iSerialNumber`, needed by the serial handler.
        self.descriptors = self.create_descriptors()

        # --- Serial-number source (selected by config) ---
        kind = config.serial_source if config else "flash_uid"
        self.serial_source = {
            "security_page": SecurityPageSerialSource(),
            "flash_uid":     FlashUidSerialSource(),
        }[kind]
        self.serial_handler = USBStreamSerialDescriptorHandler(
            self.descriptor_iSerialNumber, max_len=self.serial_source.max_len)

        # --- Control-endpoint request handlers ---
        self.ms_handler = MassStorageRequestHandler(if_num=0)

        # --- Bulk data endpoints (EP1 OUT/IN) ---
        self.out_ep = USBStreamOutEndpoint(endpoint_number=1, max_packet_size=64)
        self.in_ep  = USBStreamInEndpoint(endpoint_number=1, max_packet_size=64)

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

        # Warm-reboot trigger after a complete UF2 transfer.
        reload_slot = config.reload_slot if config else 1
        reload_idle = (
            config.reload_idle_cycles
            if (config and config.reload_idle_cycles is not None)
            else 600_000
        )
        self.warmboot = Warmboot(idle_threshold_cycles=reload_idle,
                                 slot=reload_slot)

    def create_descriptors(self):
        """ Create the descriptors we want to use for our device. """

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
                    ep.wMaxPacketSize   = 64
                    ep.bInterval        = 0

                with i.EndpointDescriptor() as ep:
                    ep.bEndpointAddress = 0x81  # EP1 IN
                    ep.bmAttributes     = 0x02  # Bulk
                    ep.wMaxPacketSize   = 64
                    ep.bInterval        = 0

        return descriptors
    

    def create_clocks(self, m, platform):
        clk_48 = Signal()
        clk_12 = Signal()
        pll_locked = Signal()

        m.submodules.pll = Instance(
            "SB_PLL40_CORE",
            p_FEEDBACK_PATH="SIMPLE",
            p_DIVR=0,
            p_DIVF=47,
            p_DIVQ=4,
            p_FILTER_RANGE=1,
            i_REFERENCECLK=platform.request("clk16", dir="i").i,
            i_RESETB=1,
            i_BYPASS=0,
            o_PLLOUTGLOBAL=clk_48,
            o_LOCK=pll_locked,
        )

        # --- Clock domains ---
        cd_usb_io = ClockDomain("usb_io")
        cd_sync = ClockDomain("sync")
        m.domains += [cd_usb_io, cd_sync]

        m.d.comb += cd_usb_io.clk.eq(clk_48)
        m.d.comb += cd_sync.clk.eq(clk_12)

        platform.add_clock_constraint(cd_usb_io.clk, 48e6)
        platform.add_clock_constraint(cd_sync.clk, 12e6)

        # --- Derive 12 MHz from 48 MHz (divide by 4) ---
        div4 = Signal(range(4))
        m.d.usb_io += div4.eq(div4 + 1)
        m.d.comb += clk_12.eq(div4[-1])

        # --- Power-on reset (hold reset until PLL locks + >3 µs BRAM errata) ---
        cd_por = ClockDomain("por", reset_less=True, local=True)
        m.domains += cd_por
        m.d.comb += cd_por.clk.eq(clk_48)

        delay = int(5 * 3e-6 * 48e6)  # ~15 µs at 48 MHz
        por_timer = Signal(range(delay + 1))
        por_ready = Signal()
        with m.If(por_timer == delay):
            m.d.por += por_ready.eq(1)
        with m.Else():
            m.d.por += por_timer.eq(por_timer + 1)

        m.d.comb += cd_usb_io.rst.eq(~por_ready | ~pll_locked)
        m.d.comb += cd_sync.rst.eq(~por_ready | ~pll_locked)

    def elaborate(self, platform):
        m = Module()

        self.create_clocks(m, platform)

        usb_direct_io = platform.request('usb')
        m.submodules.usb = usb = DomainRenamer({'usb':'sync'})(USBDevice(bus=usb_direct_io))
        m.submodules.qspi = qspi = Controller(platform.request('spi_flash_4x', dir='-'), chip_count=1, offset=0)

        m.submodules.serial_source = ss = self.serial_source

        # Add our standard control endpoint to the device.
        ep = usb.add_standard_control_endpoint(
            self.descriptors, skiplist=[self.serial_handler.handler_condition])
        ep.add_request_handler(self.serial_handler)
        ep.add_request_handler(self.ms_handler)
        m.d.comb += [
            qspi.divisor.eq(4),
            # Feed the serial source's ASCII byte stream into the handler.
            self.serial_handler.serial_data.eq(ss.data.p.data),
            self.serial_handler.serial_valid.eq(ss.data.valid),
            ss.data.ready.eq(self.serial_handler.serial_ready),
        ]

        # Bulk data endpoints.
        usb.add_endpoint(self.out_ep)
        usb.add_endpoint(self.in_ep)

        # Mass-storage / UF2 write path. SCSI and Warmboot are reset on a
        # USB bus reset (and SCSI also on a Mass-Storage class reset); a
        # bus reset mid-upload cancels any pending reload.
        m.submodules.scsi = scsi = ResetInserter(usb.reset_detected | self.ms_handler.reset)(self.scsi)
        m.submodules.uf2 = uf2 = self.uf2
        m.submodules.flash = flash = self.flash
        m.submodules.warmboot = warmboot = ResetInserter(usb.reset_detected)(self.warmboot)

        m.d.comb += [
            warmboot.arm.eq(uf2.done),
            # Any of these high means the device is mid-transaction
            # and we shouldn't reconfigure yet.
            warmboot.activity.eq(scsi.rx.valid | scsi.tx.valid | flash.qo.valid),
        ]

        wiring.connect(m, ep_out=self.out_ep.o, scsi=scsi.rx)
        wiring.connect(m, ep_in=self.in_ep.i, scsi=scsi.tx)

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
        
        
        # # TODO:: Connect DFUHanlder interface into QSPI/Flash controller
        # areas = [
        #     (0x000000, 'Application gateware'), # Alt 0 (1MB into FLASH)
        # ]

        # dfu = DFUHandler(0, [offset for offset, name in areas], runtime=False)
        # ep.add_request_handler(dfu)

        return m
