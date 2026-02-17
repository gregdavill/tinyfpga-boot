from amaranth import *
from amaranth.lib import wiring, stream, data

from usb_protocol.emitters   import DeviceDescriptorCollection
from luna.usb2               import USBDevice

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint

from blocks.qspi import Controller
from blocks.flash_uid import FlashUID
from blocks.usb.serial_handler import USBRuntimeSerialDescriptorHandler
from blocks.dfu import DFUHandler

from blocks.scsi import SCSIHandler, MassStorageRequestHandler
from blocks.uf2 import UF2Decoder
from blocks.flash import QspiFlash


class Top(Elaboratable):
    def create_descriptors(self):
        """ Create the descriptors we want to use for our device. """

        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = 0x1209
            d.idProduct          = 0x5af0

            d.bcdDevice          = 2.0

            d.iManufacturer      = "TinyFPGA"
            d.iProduct           = "Bootloader"
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

        # Create our USB device interface...
        usb_direct_io = platform.request('usb')
        m.submodules.usb = usb = DomainRenamer({'usb':'sync'})(USBDevice(bus=usb_direct_io))
        m.submodules.qspi = qspi = Controller(platform.request('spi_flash_4x', dir='-'), chip_count=1, offset=0)
        m.submodules.uuid = uuid = FlashUID()

        # Connect UUID to serial number
        descriptors = self.create_descriptors()
        handler = USBRuntimeSerialDescriptorHandler(self.descriptor_iSerialNumber, len(uuid.uuid))
        
        ms_handler = MassStorageRequestHandler(if_num=0)

        # Add our standard control endpoint to the device.
        ep = usb.add_standard_control_endpoint(descriptors, skiplist=[handler.handler_condition])

        ep.add_request_handler(handler)
        ep.add_request_handler(ms_handler)
        m.d.comb += [
            qspi.divisor.eq(4),
            handler.serial.eq(uuid.uuid),
        ]

        # Add a stream endpoint to our device.
        out_ep = USBStreamOutEndpoint(
            endpoint_number=1,
            max_packet_size=64,
        )
        usb.add_endpoint(out_ep)

        # Add a stream endpoint to our device.
        in_ep = USBStreamInEndpoint(
            endpoint_number=1,
            max_packet_size=64
        )
        usb.add_endpoint(in_ep)

        m.submodules.scsi = scsi = ResetInserter(usb.reset_detected | ms_handler.reset)(SCSIHandler(block_count=16 * 1024 * 1024 // 512, block_size=512))
        m.submodules.uf2 = uf2 = UF2Decoder()
        m.submodules.flash = flash = QspiFlash()

        wiring.connect(m, ep_out=out_ep.o, scsi=scsi.rx)
        wiring.connect(m, ep_in=in_ep.i, scsi=scsi.tx)

        with m.FSM():
            with m.State('UUID'):
                wiring.connect(m, uuid.o, qspi.i)
                wiring.connect(m, uuid.i, qspi.o)
                m.d.comb += uuid.req.eq(1)
                with m.If(uuid.valid):
                    m.next = 'USB-CONNECT'

            with m.State('USB-CONNECT'):
                m.d.comb += usb.connect.eq(1)

                wiring.connect(m, scsi.write_stream, uf2.i)
                wiring.connect(m, uf2.o, flash.i)
                wiring.connect(m, flash.qo, qspi.i)
                wiring.connect(m, flash.qi, qspi.o)
        
        
        # # TODO:: Connect DFUHanlder interface into QSPI/Flash controller
        # areas = [
        #     (0x000000, 'Application gateware'), # Alt 0 (1MB into FLASH)
        # ]

        # dfu = DFUHandler(0, [offset for offset, name in areas], runtime=False)
        # ep.add_request_handler(dfu)

        return m
