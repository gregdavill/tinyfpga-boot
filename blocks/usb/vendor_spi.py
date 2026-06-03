"""EP0 vendor-request bridge to the shared QSPI flash.

Lets a host issue raw SPI flash transactions via USB *control* transfers

Vendor requests API (bmRequestType bit6 = vendor, recipient = device):

    EXEC   (0x01, host->device): wValue = number of bytes to read back.
           Data stage = the bytes to clock out (opcode, address, payload).
           The device asserts CS#, shifts the write bytes (PutX1), clocks in
           `wValue` bytes (GetX1) into a buffer, then releases CS#
           The control-OUT status (NAK) until the SPI burst finishes.

    RESULT (0x02, device->host): returns the bytes captured by the last EXEC.

"""

from amaranth import *
from amaranth.lib import stream, data
from amaranth.lib.memory import Memory

from usb_protocol.types import USBRequestType, USBRequestRecipient
from luna.gateware.usb.usb2.request import USBRequestHandler

from blocks.qspi import Mode

REQ_EXEC   = 0x01
REQ_RESULT = 0x02

class VendorSpiHandler(USBRequestHandler):
    _WBUF = 512   # write staging: opcode + 3 address + up to a 256-byte page
    _RBUF = 256   # read staging: one 256-byte security register
    _MPS  = 64    # EP0 max packet size

    def __init__(self):
        super().__init__()
        self.qo = stream.Signature(data.StructLayout({
            "chip": range(2), "mode": Mode, "data": 8})).create()
        self.qi = stream.Signature(data.StructLayout({"data": 8})).flip().create()
        #: high while a SPI burst owns the QSPI bus (drives Top's mux).
        self.active = Signal()

    def elaborate(self, platform):
        m = Module()
        interface = self.interface
        setup = interface.setup
        rx = interface.rx
        tx = interface.tx

        m.submodules.wmem = wmem = Memory(shape=unsigned(8), depth=self._WBUF, init=[])
        m.submodules.rmem = rmem = Memory(shape=unsigned(8), depth=self._RBUF, init=[])
        w_wr = wmem.write_port(domain="usb")
        w_rd = wmem.read_port(domain="usb")
        r_wr = rmem.write_port(domain="usb")
        r_rd = rmem.read_port(domain="usb")

        wlen       = Signal(range(self._WBUF + 1))   # write bytes received
        rlen       = Signal(range(self._RBUF + 1))   # read bytes requested (wValue)
        rxidx      = Signal(range(self._WBUF + 1))   # EP0 receive index
        widx       = Signal(range(self._WBUF + 1))   # SPI write shift index
        widx_next  = Signal(range(self._WBUF + 1))
        ridx       = Signal(range(self._RBUF + 1))   # SPI read bytes captured

        # RESULT (EP0 IN) streaming bookkeeping.
        pos       = Signal(range(self._RBUF + 1))
        pos_next   = Signal(range(self._RBUF + 1))
        in_packet = Signal(range(self._MPS + 1))
        pid       = Signal(init=1)

        # EP0 <-> SPI-sequencer handshake.
        spi_start = Signal()        # pulse: kick a burst
        spi_busy  = Signal()        # high from start until the burst finishes

        m.d.comb += [
            self.qo.valid.eq(0),
            self.qi.ready.eq(0),
            w_wr.en.eq(0),
            r_wr.en.eq(0),
        ]

        # ----------------------------------------------------------------
        # SPI sequencer: drives the QSPI bus for one EXEC burst, decoupled
        # from the EP0 handshake. Owns `active` (the Top mux) and `spi_busy`.
        # ----------------------------------------------------------------
        with m.FSM(domain="usb", name="spi") as spi_fsm:
            with m.State("IDLE"):
                with m.If(spi_start):
                    m.d.usb += [widx.eq(0), ridx.eq(0)]
                    m.next = "SHIFT_W"

            with m.State("SHIFT_W"):
                m.d.comb += [
                    self.active.eq(1),
                    widx_next.eq(widx),
                    w_rd.addr.eq(widx_next),
                    self.qo.p.chip.eq(1),
                    self.qo.p.mode.eq(Mode.PutX1),
                    self.qo.p.data.eq(w_rd.data),
                    self.qo.valid.eq(widx != wlen),
                ]
                with m.If(self.qo.valid & self.qo.ready):
                    m.d.comb += widx_next.eq(widx + 1)
                    m.d.usb += widx.eq(widx_next)
                with m.If(widx == wlen):
                    with m.If(rlen != 0):
                        m.next = "SHIFT_R_REQ"
                    with m.Else():
                        m.next = "RELEASE"

            # The QSPI Controller is request/response: clock out one GetX1,
            # consume the one byte it returns, then the next (cf. SpiBridge).
            with m.State("SHIFT_R_REQ"):
                m.d.comb += [
                    self.active.eq(1),
                    self.qo.p.chip.eq(1),
                    self.qo.p.mode.eq(Mode.GetX1),
                    self.qo.valid.eq(1),
                ]
                with m.If(self.qo.ready):
                    m.next = "SHIFT_R_RESP"

            with m.State("SHIFT_R_RESP"):
                m.d.comb += [
                    self.active.eq(1),
                    r_wr.addr.eq(ridx),
                    r_wr.data.eq(self.qi.p.data),
                    self.qi.ready.eq(1),
                ]
                with m.If(self.qi.valid):
                    m.d.comb += r_wr.en.eq(1)
                    m.d.usb += ridx.eq(ridx + 1)
                    with m.If(ridx == rlen - 1):
                        m.next = "RELEASE"
                    with m.Else():
                        m.next = "SHIFT_R_REQ"

            with m.State("RELEASE"):
                m.d.comb += [
                    self.active.eq(1),
                    self.qo.p.chip.eq(0),       # chip=0 releases CS#
                    self.qo.p.mode.eq(Mode.Dummy),
                    self.qo.valid.eq(1),
                ]
                with m.If(self.qo.ready):
                    m.next = "IDLE"

        m.d.comb += spi_busy.eq(~spi_fsm.ongoing("IDLE") | spi_start)

        # ----------------------------------------------------------------
        # EP0 control handler.
        # ----------------------------------------------------------------
        with m.FSM(domain="usb", name="ep0"):
            with m.State("IDLE"):
                is_vendor = (setup.type == USBRequestType.VENDOR) & \
                            (setup.recipient == USBRequestRecipient.DEVICE)
                with m.If(setup.received & is_vendor):
                    with m.Switch(setup.request):
                        with m.Case(REQ_EXEC):
                            # `rlen` persists into the following RESULT, so
                            # latch it on EXEC only.
                            m.d.usb += [rlen.eq(setup.value), rxidx.eq(0)]
                            m.next = "RX"
                        with m.Case(REQ_RESULT):
                            m.d.usb += [pos.eq(0), in_packet.eq(0), pid.eq(1)]
                            m.next = "RESULT_CLAIM"
                        # other vendor requests: unclaimed -> fallback stalls

            # ---- EXEC: receive the write bytes, then kick the burst ----
            with m.State("RX"):
                m.d.comb += [
                    interface.claim.eq(1),
                    w_wr.addr.eq(rxidx),
                    w_wr.data.eq(rx.payload),
                ]
                with m.If(rx.valid & rx.next):
                    m.d.comb += w_wr.en.eq(1)
                    m.d.usb += rxidx.eq(rxidx + 1)
                # ACK each OUT data packet as it lands. A payload larger than
                # EP0's max packet (e.g. a 256-byte page program) arrives as
                # several packets, each ending in rx_ready_for_response.
                with m.If(interface.rx_ready_for_response):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                # The data stage is over once the host turns the bus around for
                # the IN status; latch the length and kick the SPI burst (NAK
                # this first status poll - the burst isn't done yet).
                with m.If(interface.status_requested):
                    m.d.comb += interface.handshakes_out.nak.eq(1)
                    m.d.usb += wlen.eq(rxidx)
                    m.d.comb += spi_start.eq(1)
                    m.next = "EXEC_STATUS"

            # ---- EXEC status: IN ZLP, held off until the burst completes ----
            with m.State("EXEC_STATUS"):
                m.d.comb += interface.claim.eq(1)
                with m.If(interface.status_requested):
                    with m.If(spi_busy):
                        m.d.comb += interface.handshakes_out.nak.eq(1)
                    with m.Else():
                        m.d.comb += self.send_zlp()
                        m.next = "IDLE"

            # ---- RESULT: stream the captured read bytes on EP0 IN ----
            # Mirrors USBStreamSerialDescriptorHandler: CLAIM holds the
            # interface between packets, STREAM emits one packet at a time
            # (cut at _MPS), toggling the data PID.
            with m.State("RESULT_CLAIM"):
                m.d.comb += interface.claim.eq(1)
                with m.If(interface.data_requested):
                    m.d.usb += in_packet.eq(0)
                    m.next = "RESULT_STREAM"
                with m.If(interface.status_requested):
                    m.d.comb += interface.handshakes_out.ack.eq(1)
                    m.next = "IDLE"

            with m.State("RESULT_STREAM"):
                # min(rlen, wLength) bytes per USB 2.0 §9.3.5.
                transfer_last = (pos == rlen - 1) | (pos == setup.length - 1)
                packet_last = transfer_last | (in_packet == self._MPS - 1)
                m.d.comb += [
                    interface.claim.eq(1),
                    pos_next.eq(pos),
                    r_rd.addr.eq(pos_next),
                    interface.tx_data_pid.eq(pid),
                    tx.valid.eq(1),
                    tx.payload.eq(r_rd.data),
                    tx.first.eq(in_packet == 0),
                    tx.last.eq(packet_last),
                ]
                with m.If(tx.valid & tx.ready):
                    m.d.comb += pos_next.eq(pos + 1),
                    with m.If(transfer_last):
                        m.next = "RESULT_CLAIM"
                    with m.Elif(in_packet == self._MPS - 1):
                        m.d.usb += [pos.eq(pos + 1), pid.eq(~pid)]
                        m.next = "RESULT_CLAIM"
                    with m.Else():
                        m.d.usb += [pos.eq(pos + 1),
                                    in_packet.eq(in_packet + 1)]

        return m
