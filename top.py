from amaranth import *
from amaranth.lib import wiring, io
from blocks.ports import PortGroup

from usb_protocol.emitters   import DeviceDescriptorCollection
from luna.usb2               import USBDevice

from config import SerialSource

from blocks.qspi import Controller
from blocks.serial_source import FlashUidSerialSource, SecurityPageSerialSource
from blocks.usb.serial_handler import USBStreamSerialDescriptorHandler
from blocks.usb.vendor_spi import VendorSpiHandler

from backends import BACKENDS
from staysource import NoValidAppStaySource
from staysource.no_valid_app import SYNC_WORDS


class Top(Elaboratable):
    def __init__(self, config=None):
        self.config = config

        hs = bool(config) and config.platform.usb_phy == "ulpi_hs"

        # --- Serial-number source (selected by config) ---
        kind = config.serial_source if config else SerialSource.FLASH_UID
        if kind == SerialSource.SECURITY_PAGE:
            self.serial_source = SecurityPageSerialSource(
                addr_offset_bits=config.security_page_addr_offset_bits if config else 0)
        else:
            self.serial_source = FlashUidSerialSource()

        # --- USB personality (the active backend), selected by config ---
        self.backend = BACKENDS[config.backend](config, hs=hs)

        # --- Auto-boot: pluggable "stay in the bootloader" sources. ---
        factories = list(config.stay_sources) if config else []
        self.auto_boot = bool(factories)
        self.stay_sources = []
        if self.auto_boot:
            self.stay_sources.append(NoValidAppStaySource(
                app_offset=config.reload_image_offset or 0,
                sync_word=SYNC_WORDS[config.platform.fpga_family]))
            self.stay_sources += [make() for make in factories]

        # --- USB descriptors + serial-number control handler ---
        self.descriptors = self.create_descriptors(hs)
        self.serial_handler = USBStreamSerialDescriptorHandler(
            self.descriptor_iSerialNumber, max_len=self.serial_source.max_len)

        # --- Vendor EP0 bridge ---
        self.vendor_spi = VendorSpiHandler()

    def create_descriptors(self, hs):
        """ Create the descriptors we want to use for our device. The device
        descriptor's identity comes from the active backend; the configuration
        descriptor's interface(s)/endpoints are filled in by the backend. """

        # High-speed bulk endpoints must advertise 512-byte max packets;
        # the other-speed (full-speed) view always advertises 64.
        bulk_mps = 512 if hs else 64

        idVendor, idProduct = self.backend.usb_ids
        bClass, bSubclass, bProtocol = self.backend.device_class

        descriptors = DeviceDescriptorCollection()

        with descriptors.DeviceDescriptor() as d:
            d.idVendor           = idVendor
            d.idProduct          = idProduct

            d.bDeviceClass       = bClass
            d.bDeviceSubclass    = bSubclass
            d.bDeviceProtocol    = bProtocol

            d.bcdDevice          = 2.0
            d.bMaxPacketSize0    = 64

            d.iManufacturer      = self.config.manufacturer if self.config else "TinyFPGA"
            model                = self.config.model if self.config else "Bootloader"
            d.iProduct           = f"{model} ({self.backend.personality})"
            d.iSerialNumber      = ""

            d.bNumConfigurations = 1

        # Store string descriptor index
        self.descriptor_iSerialNumber = d.fields['iSerialNumber']

        with descriptors.ConfigurationDescriptor() as c:
            self.backend.populate_configuration(c, bulk_mps=bulk_mps)

        # A high-speed-capable device must additionally answer
        # GET_DESCRIPTOR(DEVICE_QUALIFIER) and GET_DESCRIPTOR(OTHER_SPEED_
        # CONFIGURATION) describing how it would behave at the other speed
        # [USB2.0 9.6.2/9.6.4]. LUNA's GET_DESCRIPTOR ROM stalls any type it
        # wasn't given, so we add them explicitly here.
        if hs:
            from usb_protocol.emitters.descriptors.standard import (
                DeviceQualifierDescriptor, ConfigurationDescriptorEmitter,
            )

            dq = DeviceQualifierDescriptor()
            dq.bcdUSB            = 2.0
            dq.bDeviceClass      = bClass
            dq.bDeviceSubclass   = bSubclass
            dq.bDeviceProtocol   = bProtocol
            dq.bMaxPacketSize0   = 64      # must match the device descriptor
            dq.bNumConfigurations = 1
            descriptors.add_descriptor(dq)  # type 6, derived from byte[1]

            # Other-speed configuration: identical interface(s) but with the
            # full-speed (64-byte) bulk endpoints. Emit a normal configuration
            # descriptor, then rewrite its type byte 2 -> 7.
            other = ConfigurationDescriptorEmitter(collection=descriptors)
            self.backend.populate_configuration(other, bulk_mps=64)
            blob = bytearray(other.emit())
            blob[1] = 7  # CONFIGURATION (2) -> OTHER_SPEED_CONFIGURATION (7)
            descriptors.add_descriptor(bytes(blob), descriptor_type=7)

        return descriptors


    def elaborate(self, platform):
        m = Module()

        # Clock/reset generation is platform-intrinsic
        platform.create_clocks(m)

        usb_bus = platform.request(platform.default_usb_connection)
        m.submodules.usb = usb = DomainRenamer({'usb':'sync'})(USBDevice(bus=usb_bus))

        # QSPI flash; the clock routing is platform-specific.
        if platform.flash_clk == "usrmclk":
            # ECP5 only: the flash clock is the dedicated config MCLK,
            # reachable only via the USRMCLK primitive.
            flash_io = platform.request('spi_flash_4x', dir='-')
            flash_clk = io.SimulationPort("o", 1)
            clk_o = Signal()
            m.d.sync += clk_o.eq(flash_clk.o)
            qspi_ports = PortGroup(cs=flash_io.cs, clk=flash_clk, dq=flash_io.dq)
            m.submodules.qspi = qspi = Controller(qspi_ports, chip_count=1, offset=0)
            m.submodules.usrmclk = Instance(
                "USRMCLK",
                i_USRMCLKI=clk_o,      # fabric SPI clock -> config flash CCLK
                i_USRMCLKTS=Const(0),        # 0 = drive the clock (not tri-stated)
            )
        else:
            # Ordinary I/O pad for the flash clock (full DDR rate). The flash
            # resource must include a `clk` subsignal the controller drives.
            m.submodules.qspi = qspi = Controller(platform.request('spi_flash_4x', dir='-'), chip_count=1, offset=0)

        m.submodules.serial_source = ss = self.serial_source

        for idx, src in enumerate(self.stay_sources):
            m.submodules[f"stay_{idx}"] = src

        # Add our standard control endpoint to the device.
        ep = usb.add_standard_control_endpoint(
            self.descriptors, skiplist=[self.serial_handler.handler_condition])
        ep.add_request_handler(self.serial_handler)
        ep.add_request_handler(self.vendor_spi)
        for handler in self.backend.request_handlers():
            ep.add_request_handler(handler)
        m.d.comb += [
            qspi.divisor.eq(4),
            # Feed the serial source's ASCII byte stream into the handler.
            self.serial_handler.serial_data.eq(ss.data.p.data),
            self.serial_handler.serial_valid.eq(ss.data.valid),
            ss.data.ready.eq(self.serial_handler.serial_ready),
        ]

        # Backend bulk/interrupt endpoints.
        for endpoint in self.backend.endpoints():
            usb.add_endpoint(endpoint)

        # Backend datapath: registers its submodules + wiring, exposes the
        # QSPI-facing streams, and reports the reconfigure arm/activity.
        rc = self.backend.build(m, usb=usb)

        # Dual-bank boards drive `cfg_ctrl` (the FLASH/RAM config-source latch)
        # from the backend's bank controller.
        if self.config and self.config.has_ram_bank:
            cfg_ctrl = platform.request("cfg_ctrl", dir="o")
            m.d.comb += cfg_ctrl.o.eq(self.backend.cfg_ctrl_o)

        # Status indicator — the platform owns its board-specific LED(s).
        platform.create_status_led(m, self.backend.status)

        # System-reconfigure trigger; the platform owns the primitive.
        # A bus reset cancels a pending reload, and `activity` keeps
        # it from firing mid-transaction.
        reload_slot = self.config.reload_slot if self.config else 1
        reload_idle = (
            self.config.reload_idle_cycles
            if (self.config and self.config.reload_idle_cycles is not None)
            else 600_000
        )
        # `por_boot` latches when the auto-boot decision selects the app:
        # it arms the same reconfigure trigger the backend uses post-upload.
        por_boot = Signal()
        platform.create_reconfigure(
            m,
            arm=rc.arm | por_boot,
            activity=rc.activity,
            reset=usb.connect & usb.reset_detected,
            slot=reload_slot,
            idle_cycles=reload_idle,
        )

        # Flash consumers sequenced over the shared QSPI bus at boot: the serial
        # source first, then any flash-backed stay sources. Each is granted the
        # bus in turn (req held, wait for done).
        flash_clients = [ss] + [s for s in self.stay_sources if s.needs_flash]
        sel = Signal(range(len(flash_clients) + 1))

        with m.FSM():
            with m.State('BOOT-READ'):
                # Read the serial source (and any flash stay sources) over QSPI
                # before USB comes up.
                with m.Switch(sel):
                    for idx, client in enumerate(flash_clients):
                        with m.Case(idx):
                            wiring.connect(m, client.o, qspi.i)
                            wiring.connect(m, client.i, qspi.o)
                            m.d.comb += client.req.eq(1)
                            with m.If(client.done):
                                m.d.sync += sel.eq(idx + 1)
                with m.If(sel == len(flash_clients)):
                    m.next = 'BOOT-DECIDE'

            with m.State('BOOT-DECIDE'):
                if self.auto_boot:
                    # Stay (enumerate) if any source vetoes; otherwise auto-boot.
                    stay = Cat(*(s.stay for s in self.stay_sources))
                    with m.If(stay.any()):
                        m.next = 'USB-CONNECT'
                    with m.Else():
                        m.d.sync += por_boot.eq(1)
                        m.next = 'REBOOT-WAIT'
                else:
                    m.next = 'USB-CONNECT'

            with m.State('REBOOT-WAIT'):
                # Don't enumerate; create_reconfigure (armed via por_boot) reboots
                # into the slot-1 app once the idle window elapses.
                pass

            with m.State('USB-CONNECT'):
                m.d.comb += usb.connect.eq(1)

                # Share the QSPI bus: the vendor EP0 bridge grabs it while a
                # provisioning burst is in flight, otherwise the backend's
                # datapath owns it (the two are never busy at once).
                vs = self.vendor_spi
                with m.If(vs.active):
                    wiring.connect(m, vs.qo, qspi.i)
                    wiring.connect(m, qspi.o, vs.qi)
                with m.Else():
                    wiring.connect(m, self.backend.qo, qspi.i)
                    wiring.connect(m, self.backend.qi, qspi.o)

        return m
