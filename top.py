from amaranth import *
from amaranth.lib import wiring

from usb_protocol.emitters   import DeviceDescriptorCollection
from luna.usb2               import USBDevice, USBStreamOutEndpoint, USBStreamInEndpoint

from blocks.qspi import Controller
from blocks.flash_uid import FlashUID
from blocks.usb_serialnumber import USBSerialNumberHandler
from blocks.dfu import DFUHandler


class Top(Elaboratable):
    def create_descriptors(self):
        """ Create the descriptors we want to use for our device. """

        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = 0x1209
            d.idProduct          = 0x5af0

            d.bcdDevice          = 1.0

            d.iManufacturer      = "TinyFPGA"
            d.iProduct           = "Bootloader"
            d.iSerialNumber      = ""

            d.bNumConfigurations = 1

        # Store string descriptor index
        self.descriptor_iSerialNumber = d.fields['iSerialNumber']

        with descriptors.ConfigurationDescriptor() as c:

            c.bMaxPower = 0 # Self powered

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
        handler = USBSerialNumberHandler(self.descriptor_iSerialNumber, len(uuid.uuid))
        
        # Add our standard control endpoint to the device.
        ep = usb.add_standard_control_endpoint(descriptors, skiplist=[handler.skip])

        ep.add_request_handler(handler)
        m.d.comb += [
            qspi.divisor.eq(4),
            handler.serial.eq(uuid.uuid),
        ]

        with m.FSM():
            with m.State('UUID'):
                wiring.connect(m, uuid.o, qspi.i)
                wiring.connect(m, uuid.i, qspi.o)
                m.d.comb += uuid.req.eq(1)
                with m.If(uuid.valid):
                    m.next = 'USB-CONNECT'

            with m.State('USB-CONNECT'):
                m.d.comb += usb.connect.eq(1)
        
        
        areas = [
            (0x000000, 'Application gateware'), # Alt 0 (1MB into FLASH)
        ]

        # TODO:: Connect DFUHanlder interface into QSPI/Flash controller
        dfu = DFUHandler(0, [offset for offset, name in areas], runtime=False)
        ep.add_request_handler(dfu)

        return m
