from amaranth import *
from amaranth.lib import stream, data
from usb_protocol.types import USBRequestType, USBStandardRequests, USBRequestRecipient
from enum import Enum

from luna.gateware.usb.usb2.request import USBRequestHandler
from luna.gateware.usb.stream import USBInStreamInterface
from luna.gateware.stream.generator import StreamSerializer
from luna.gateware.memory import TransactionalizedFIFO


class DFUState(Enum):
    appIDLE = 0
    appDETACH = 1
    dfuIDLE = 2
    dfuDNLOAD_SYNC = 3
    dfuDNBUSY = 4
    dfuDNLOAD_IDLE = 5
    dfuMANIFEST_SYNC = 6
    dfuMANIFEST = 7
    dfuMANIFEST_WAIT_RESET = 8
    dfuUPLOAD_IDLE = 9
    dfuERROR = 10


class DFUHandler(USBRequestHandler):
    def __init__(self, if_num, areas):
        super().__init__()

        # (addr, data) byte stream feeding the flash writer; matches
        # QspiFlash.i so Top/`build` can `wiring.connect` it directly.
        self.source = stream.Signature(
            data.StructLayout({"addr": 24, "data": 8})
        ).create()

        self.if_num = if_num

        # handle SET_INTERFACE (alt-setting selection)
        self.handler_condition = lambda setup: \
            (setup.type == USBRequestType.STANDARD) & \
            (setup.recipient == USBRequestRecipient.INTERFACE) & \
            (setup.index == self.if_num) & \
            (setup.request == USBStandardRequests.SET_INTERFACE)

        self.addr = Signal(24)

        self.areas = Array(areas)
        self.area_sel = Signal(range(len(self.areas)))

        self.new_request = Signal()
        #: 1-cycle strobe on the zero-length final DNLOAD (download complete).
        self.manifest = Signal()
        #: 1-cycle strobe on the first DNLOAD of a session (fresh download).
        self.download_start = Signal()
        self.request_done = Signal()
        self.state = Signal(8, reset=DFUState.dfuIDLE)

    def handle_set_interface(self, m):
        m.d.usb += self.area_sel.eq(self.interface.setup.value)

        with m.If(self.interface.status_requested):
            m.d.comb += self.send_zlp()
            m.d.comb += self.request_done.eq(1)

    def handle_get_status(self, m):
        m.d.comb += [
            self.transmitter.stream.attach(self.interface.tx),
            Cat(self.transmitter.data).eq(
                Cat(
                    C(0, 8),
                    C(0, 24),
                    self.state,
                    C(0, 8)
                )
            ),
            self.transmitter.start.eq(self.interface.data_requested),
        ]

        with m.If(self.interface.status_requested):
            m.d.comb += self.interface.handshakes_out.ack.eq(1)
            m.d.comb += self.request_done.eq(1)

    def handle_dfu_detach(self, m):
        with m.If(self.interface.status_requested):
            m.d.comb += self.send_zlp()
            m.d.comb += self.request_done.eq(1)

    def handle_dnload(self, m):
        fifo = self.fifo

        interface = self.interface
        rx = self.interface.rx

        m.d.comb += [
            fifo.write_data.eq(rx.payload),
            fifo.write_en.eq(rx.next & rx.valid),
            fifo.write_discard.eq(interface.rx_invalid),
            fifo.write_commit.eq(interface.rx_ready_for_response),
        ]

        with m.If(self.new_request):
            with m.If(self.interface.setup.length > 0):
                m.d.usb += self.state.eq(DFUState.dfuDNLOAD_IDLE)
            with m.Else():
                m.d.usb += self.state.eq(DFUState.dfuIDLE)

            # A fresh DNLOAD restarts at the slot base; continuation chunks
            # (state already dfuDNLOAD_IDLE) keep incrementing `addr`.
            with m.If(self.state == DFUState.dfuIDLE):
                m.d.usb += self.addr.eq(self.areas[self.area_sel])
                m.d.usb += self.download_start.eq(1)

            # A zero-length DNLOAD ends the transfer (manifestation).
            with m.If(self.interface.setup.length == 0):
                m.d.usb += self.manifest.eq(1)

        with m.If(self.interface.rx_ready_for_response):
            m.d.comb += self.interface.handshakes_out.ack.eq(1)

        with m.If(self.interface.status_requested):
            with m.If(fifo.empty | (fifo.space_available >= 256)):
                m.d.comb += self.send_zlp()
                m.d.comb += self.request_done.eq(1)

            with m.Else():
                m.d.comb += self.interface.handshakes_out.nak.eq(1)

    def transition(self, m):
        setup = self.interface.setup

        with m.If(self.request_done):
            m.next = "IDLE"

        targeting_if = (setup.recipient == USBRequestRecipient.INTERFACE) & (
            setup.index == self.if_num
        )

        with m.If(setup.received & targeting_if):
            m.next = "DISPATCH"

    def elaborate(self, platform):
        m = Module()

        m.submodules.fifo = self.fifo = TransactionalizedFIFO(
            width=8, depth=512, domain="usb"
        )

        fifo = self.fifo
        interface = self.interface

        m.d.comb += [
            self.source.p.data.eq(fifo.read_data),
            self.source.valid.eq(~fifo.empty),
            fifo.read_en.eq(self.source.ready),
            fifo.read_commit.eq(1),
            self.source.p.addr.eq(self.addr),
        ]

        with m.If(self.source.valid & self.source.ready):
            m.d.usb += self.addr.eq(self.addr + 1)

        m.submodules.transmitter = self.transmitter = StreamSerializer(
            data_length=6, domain="usb", stream_type=USBInStreamInterface
        )

        setup = self.interface.setup

        m.d.usb += [
            self.new_request.eq(0),
            self.manifest.eq(0),
            self.download_start.eq(0),
        ]

        with m.FSM(domain="usb"):
            with m.State("IDLE"):
                self.transition(m)

            with m.State("DISPATCH"):
                m.d.usb += self.new_request.eq(1)

                with m.If(setup.type == USBRequestType.STANDARD):
                    with m.Switch(setup.request):
                        with m.Case(USBStandardRequests.SET_INTERFACE):
                            m.next = "SET_INTERFACE"

                with m.If(setup.type == USBRequestType.CLASS):
                    with m.Switch(setup.request):
                        with m.Case(0):  # DFU_DETACH
                            m.next = "DFU_DETACH"
                        with m.Case(1):  # DFU_DNLOAD
                            m.next = "DFU_DNLOAD"
                        with m.Case(3):  # DFU_GETSTATUS
                            m.next = "DFU_GETSTATUS"

            with m.State("SET_INTERFACE"):
                m.d.comb += interface.claim.eq(1)
                self.handle_set_interface(m)
                self.transition(m)

            with m.State("DFU_DETACH"):
                m.d.comb += interface.claim.eq(1)
                self.handle_dfu_detach(m)
                self.transition(m)

            with m.State("DFU_DNLOAD"):
                m.d.comb += interface.claim.eq(1)
                self.handle_dnload(m)
                self.transition(m)

            with m.State("DFU_GETSTATUS"):
                m.d.comb += interface.claim.eq(1)
                self.handle_get_status(m)
                self.transition(m)

        return m
