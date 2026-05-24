from amaranth import *
from amaranth.lib.memory import Memory
from usb_protocol.types import USBStandardRequests, USBRequestType, USBRequestRecipient, DescriptorTypes

from luna.gateware.usb.usb2.request import USBRequestHandler


class USBStreamSerialDescriptorHandler(USBRequestHandler):
    """Serve a string descriptor from a byte stream.

    This handler buffers an *ASCII* string arriving on a byte stream into 
    a small memory and emits it verbatim as a UTF-16LE USB string descriptor. 
    It pairs with ``SecurityPage``/``FlashUID`` to provide a unique serial
    from various sources.

    The stream is consumed on the `sync` domain and written into memory; 
    the descriptor is served on the `usb` domain. The serial is captured
    at boot, before the device is ready for enumeration.
    """

    def __init__(self, idx, max_len=36, max_packet_size=64):
        super().__init__()

        self.max_len = max_len
        self.max_packet_size = max_packet_size

        # Byte-stream sink for the serial characters (sync domain).
        self.serial_valid = Signal()
        self.serial_data  = Signal(8)
        self.serial_ready = Signal()

        self.handler_condition = lambda setup: \
            (setup.type == USBRequestType.STANDARD) & \
            (setup.recipient == USBRequestRecipient.DEVICE) & \
            (setup.request == USBStandardRequests.GET_DESCRIPTOR) & \
            (setup.value == (DescriptorTypes.STRING << 8) | idx)

    def elaborate(self, _):
        m = Module()

        m.submodules.mem = mem = Memory(
            shape=unsigned(8), depth=self.max_len, init=[])
        wport = mem.write_port(domain="sync")
        rport = mem.read_port(domain="comb")          # async read for the FSM

        # --- Capture side (sync): drop bytes once the buffer is full ---
        length = Signal(range(self.max_len + 1))
        m.d.comb += [
            self.serial_ready.eq(1),
            wport.addr.eq(length),
            wport.data.eq(self.serial_data),
        ]
        with m.If(self.serial_valid & (length < self.max_len)):
            m.d.comb += wport.en.eq(1)
            m.d.sync += length.eq(length + 1)

        # --- Descriptor side (usb): bLength, type, then UTF-16LE chars ---
        setup = self.interface.setup
        tx = self.interface.tx

        # Total descriptor length: 2-byte header + 2 bytes per ASCII char.
        data_length = 2 + (length << 1)

        # `pos` is the byte offset within the *whole* descriptor and must
        # persist across packets: `data_requested` pulses once per IN
        # token, `in_packet` counts bytes within the current packet so 
        # we cut it at exactly max_packet_size (the host then issues 
        # another IN for the rest).
        pos = Signal(range(2 + 2 * self.max_len + 1))
        in_packet = Signal(range(self.max_packet_size + 1))

        char_idx = (pos - 2) >> 1
        m.d.comb += rport.addr.eq(char_idx)
        data = Signal(8)
        with m.If(pos == 0):
            m.d.comb += data.eq(data_length)
        with m.Elif(pos == 1):
            m.d.comb += data.eq(3)
        with m.Elif(~pos[0]):
            m.d.comb += data.eq(rport.data)           # even: ASCII char
        # odd pos: high byte of the UTF-16 code unit stays 0.

        # End of the whole transfer: descriptor exhausted, or the host's
        # requested wLength reached (USB 2.0 §9.3.5 — return min of the two).
        transfer_last = (pos == data_length - 1) | (pos == setup.length - 1)
        # End of the current packet: transfer end, or max_packet_size bytes.
        packet_last = transfer_last | (in_packet == self.max_packet_size - 1)

        # The data-stage PID toggles per packet (DATA1, DATA0, …). LUNA's
        # transmitter frames one packet per first..last run and stamps it
        # with this toggle, so a >1-packet descriptor needs us to drive it.
        pid = Signal(init=1)
        m.d.comb += [
            self.interface.tx_data_pid.eq(pid),
            tx.first.eq((in_packet == 0) & tx.valid),
            tx.last.eq(packet_last & tx.valid),
        ]

        with m.FSM(domain='usb'):
            with m.State('IDLE'):
                with m.If(setup.received & self.handler_condition(setup)):
                    m.d.usb += [pos.eq(0), pid.eq(1)]
                    m.next = 'CLAIM'

            # Between packets: hold the claim and wait for the next IN
            # (data_requested) or the host's status-stage OUT.
            with m.State('CLAIM'):
                m.d.comb += self.interface.claim.eq(1)
                with m.If(self.interface.data_requested):
                    m.d.usb += in_packet.eq(0)
                    m.next = 'STREAM'
                with m.If(self.interface.status_requested):
                    m.d.comb += self.interface.handshakes_out.ack.eq(1)
                    m.next = 'IDLE'

            with m.State('STREAM'):
                m.d.comb += [
                    self.interface.claim.eq(1),
                    tx.valid.eq(1),
                    tx.payload.eq(data),
                ]
                with m.If(tx.ready):
                    with m.If(transfer_last):
                        m.next = 'CLAIM'                  # done; await status
                    with m.Elif(in_packet == self.max_packet_size - 1):
                        # Packet full, more to come: advance, flip the data
                        # PID, and wait for the host's next IN.
                        m.d.usb += [pos.eq(pos + 1), pid.eq(~pid)]
                        m.next = 'CLAIM'
                    with m.Else():
                        m.d.usb += [pos.eq(pos + 1),
                                    in_packet.eq(in_packet + 1)]

        return m

