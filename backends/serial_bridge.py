"""TinyFPGA serial bootloader backend — a CDC-ACM USB->SPI bridge.

Presents a USB CDC-ACM serial port whose bulk data endpoints feed a
`SpiBridge` that translates the host's command framing into raw
transactions on the shared QSPI flash.

The descriptor layout is the canonical CDC composite (IAD + Communications
interface #0 with an unused interrupt-IN notification endpoint, + Data
interface #1 with the bulk pair).
"""

from amaranth import ResetInserter
from amaranth.lib import wiring

from usb_protocol.emitters.descriptors import cdc
from luna.gateware.usb.devices.acm import ACMRequestHandlers

from blocks.luna_wrapper import USBStreamInEndpoint, USBStreamOutEndpoint
from blocks.spi_bridge import SpiBridge

from . import Backend, Reconfig


class SerialBridgeBackend(Backend):
    # Composite device using an Interface Association Descriptor.
    device_class = (0xEF, 0x02, 0x01)

    _NOTIFY_EP = 2   # interrupt IN (declared, never driven)
    _DATA_EP   = 1   # bulk IN/OUT

    def __init__(self, config, *, hs):
        super().__init__(config, hs=hs)

        # tinyprog scans for 1d50:6130 by default; pin it regardless of the
        # board's UF2 IDs so the stock programmer works with no flags.
        self.usb_ids = (0x1d50, 0x6130)

        bulk_mps = 512 if hs else 64
        self._notify_mps = 8

        self.notify_ep   = USBStreamInEndpoint(
            endpoint_number=self._NOTIFY_EP, max_packet_size=self._notify_mps)
        self.data_in_ep  = USBStreamInEndpoint(
            endpoint_number=self._DATA_EP, max_packet_size=bulk_mps)
        self.data_out_ep = USBStreamOutEndpoint(
            endpoint_number=self._DATA_EP, max_packet_size=bulk_mps)

        self.bridge = SpiBridge()

    def populate_configuration(self, c, *, bulk_mps):
        c.bMaxPower = 100

        # Both interfaces belong to one CDC function (helps Windows).
        with c.InterfaceAssociationDescriptor() as ia:
            ia.bFirstInterface   = 0
            ia.bInterfaceCount   = 2
            ia.bFunctionClass    = 0x02  # CDC
            ia.bFunctionSubClass = 0x02  # ACM
            ia.bFunctionProtocol = 0x01  # AT commands

        # Communications-class interface: functional descriptors + an unused
        # interrupt-IN notification endpoint.
        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = 0
            i.bInterfaceClass    = 0x02  # CDC
            i.bInterfaceSubclass = 0x02  # ACM
            i.bInterfaceProtocol = 0x01  # AT commands
            i.iInterface = "TinyFPGA Bootloader"

            i.add_subordinate_descriptor(cdc.HeaderDescriptorEmitter())

            cm = cdc.CallManagementFunctionalDescriptorEmitter()
            cm.bDataInterface = 1
            i.add_subordinate_descriptor(cm)

            i.add_subordinate_descriptor(cdc.ACMFunctionalDescriptorEmitter())

            union = cdc.UnionFunctionalDescriptorEmitter()
            union.bControlInterface      = 0
            union.bSubordinateInterface0 = 1
            i.add_subordinate_descriptor(union)

            with i.EndpointDescriptor() as e:
                e.bEndpointAddress = 0x80 | self._NOTIFY_EP
                e.bmAttributes     = 0x03  # Interrupt
                e.wMaxPacketSize   = self._notify_mps
                e.bInterval        = 16

        # Data-class interface: the bulk pair tinyprog uses.
        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber   = 1
            i.bInterfaceClass    = 0x0a  # CDC data
            i.bInterfaceSubclass = 0x00
            i.bInterfaceProtocol = 0x00

            with i.EndpointDescriptor() as e:
                e.bEndpointAddress = 0x80 | self._DATA_EP  # bulk IN
                e.bmAttributes     = 0x02
                e.wMaxPacketSize   = bulk_mps
                e.bInterval        = 0

            with i.EndpointDescriptor() as e:
                e.bEndpointAddress = self._DATA_EP          # bulk OUT
                e.bmAttributes     = 0x02
                e.wMaxPacketSize   = bulk_mps
                e.bInterval        = 0

    def request_handlers(self):
        # ACKs SET_LINE_CODING, stalls the rest — enough for all major OSes.
        return [ACMRequestHandlers()]

    def endpoints(self):
        return [self.notify_ep, self.data_in_ep, self.data_out_ep]

    def build(self, m, *, usb):
        m.submodules.bridge = bridge = ResetInserter(usb.reset_detected)(self.bridge)

        wiring.connect(m, ep_out=self.data_out_ep.o, bridge=bridge.rx)
        wiring.connect(m, ep_in=self.data_in_ep.i,  bridge=bridge.tx)

        # QSPI bus: Top muxes these against the serial source inside USB-CONNECT.
        self.qo = bridge.qo
        self.qi = bridge.qi

        return Reconfig(arm=bridge.boot, activity=bridge.activity)
