"""USB 2.0 Full-Speed host model for cocotb.

This is intentionally a flat, readable sketch - not a feature-complete
host stack. It models the wire protocol at the D+/D- level so the
DUT's `GatewarePHY` is exercised along with everything above it.

Not currently handled:

* SE0 reset sequencing past a basic 10 ms pulse
* split transactions, suspend/resume, remote wakeup
* low-speed signalling
* PRE PIDs, isochronous, NYET / PING (HS only)
* tolerance for the device transmitting J/K with non-zero skew

"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import cocotb
from cocotb.triggers import Timer, RisingEdge, ReadOnly
from cocotb.clock import Clock
from cocotb.utils import get_sim_time

from .dut_pins import attach as attach_pins, release


# ----------------------------------------------------------------------
# Timing
# ----------------------------------------------------------------------

def _level(signal) -> int:
    """Read a 1-bit inout pad as 0 / 1 / 1 (treats Z as J for pullup-driven idle)."""
    val = signal.value
    try:
        return int(val) & 1
    except (ValueError, TypeError):
        # 'z' / 'x' - caller should typically be in a window where the
        # bus is resolved. Treat as idle J to avoid spurious K detection.
        return 1


# ----------------------------------------------------------------------
# PIDs
# ----------------------------------------------------------------------

class PID(enum.IntEnum):
    OUT     = 0b0001
    IN      = 0b1001
    SOF     = 0b0101
    SETUP   = 0b1101
    DATA0   = 0b0011
    DATA1   = 0b1011
    ACK     = 0b0010
    NAK     = 0b1010
    STALL   = 0b1110

    def encoded(self) -> int:
        """PID byte = pid | (~pid << 4)."""
        return self.value | ((self.value ^ 0xF) << 4)


# ----------------------------------------------------------------------
# CRCs
# ----------------------------------------------------------------------

def crc5(data: int, bits: int = 11) -> int:
    """CRC5 over `bits` bits of `data`, LSB first. Polynomial 0x05."""
    crc = 0x1F
    for i in range(bits):
        bit = (data >> i) & 1
        if (crc ^ bit) & 1:
            crc = (crc >> 1) ^ 0x14
        else:
            crc >>= 1
    return (~crc) & 0x1F


def crc16(payload: bytes) -> int:
    """USB CRC16 over a byte payload, LSB first. Polynomial 0x8005."""
    crc = 0xFFFF
    for byte in payload:
        for i in range(8):
            bit = (byte >> i) & 1
            if (crc ^ bit) & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return (~crc) & 0xFFFF


# ----------------------------------------------------------------------
# NRZI + bit-stuffing
# ----------------------------------------------------------------------

def nrzi_encode(bits: list[int], start_level: int = 1) -> list[int]:
    """NRZI: 0 → toggle, 1 → hold. `start_level` is the J/K state we
    leave the line in after SYNC's last bit (the receiver expects J)."""
    out = []
    level = start_level
    for b in bits:
        if b == 0:
            level ^= 1
        out.append(level)
    return out


def bit_stuff(bits: list[int]) -> list[int]:
    """After six consecutive 1s, insert a 0 (which forces a transition)."""
    out, run = [], 0
    for b in bits:
        out.append(b)
        if b == 1:
            run += 1
            if run == 6:
                out.append(0)
                run = 0
        else:
            run = 0
    return out


def bit_unstuff(bits: list[int]) -> list[int]:
    out, run = [], 0
    i = 0
    while i < len(bits):
        b = bits[i]
        out.append(b)
        if b == 1:
            run += 1
            if run == 6:
                # The next bit is a stuff bit - skip it.
                i += 1
                run = 0
        else:
            run = 0
        i += 1
    return out


def nrzi_decode(levels: list[int], start_level: int = 1) -> list[int]:
    bits = []
    prev = start_level
    for lvl in levels:
        bits.append(1 if lvl == prev else 0)
        prev = lvl
    return bits


# ----------------------------------------------------------------------
# Packet assembly
# ----------------------------------------------------------------------

SYNC_BYTE = 0x80  # KJKJKJKK on the wire = LSB-first 0000_0001

def bytes_to_bits_lsb(data: bytes) -> list[int]:
    out = []
    for byte in data:
        for i in range(8):
            out.append((byte >> i) & 1)
    return out


def bits_to_bytes_lsb(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for j in range(8):
            byte |= (bits[i + j] & 1) << j
        out.append(byte)
    return bytes(out)


def build_token(pid: PID, address: int, endpoint: int) -> bytes:
    word = (address & 0x7F) | ((endpoint & 0xF) << 7)
    word |= (crc5(word, bits=11) & 0x1F) << 11
    return bytes([pid.encoded(), word & 0xFF, (word >> 8) & 0xFF])


def build_data(pid: PID, payload: bytes) -> bytes:
    crc = crc16(payload)
    return bytes([pid.encoded()]) + payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_handshake(pid: PID) -> bytes:
    return bytes([pid.encoded()])


# ----------------------------------------------------------------------
# USB host
# ----------------------------------------------------------------------

@dataclass
class EndpointToggle:
    out: int = 0
    in_:  int = 0


class USBHost:
    """Cocotb USB FS host.

    The host owns the bus while `usb_d_p_oe`/`usb_d_n_oe` are driven
    high on its side. The DUT GatewarePHY drives the lines back when
    it transmits. We sample on the bit-time grid; sufficient for 
    verifying packet content.
    """

    def __init__(self, dut):
        self.dut = dut
        self.pins = attach_pins(dut)
        # bMaxPacketSize0 - populated after the device descriptor has
        # been read
        self.max_packet0 = 8
        # FS bit time in picoseconds. 

        # In sim the device's 48 MHz comes from cocotb's 20834 ps clk
        # (under the nominal 20833.33 ps) actual FS bit is 
        # 4 × 20834 = 83336 ps
        self.bit_ps      = 83336
        self.half_bit_ps = self.bit_ps // 2
        self.bit_ns      = self.bit_ps // 1000   # kept for callers that still pass ns
        self.half_bit_ns = self.bit_ns // 2
        self.address = 0                     # device starts at 0
        self.toggles: dict[int, EndpointToggle] = {}

    # ------------------------------------------------------------------
    # Bring-up
    # ------------------------------------------------------------------

    async def start(self):
        """Start the reference clock and idle the bus by releasing the
        host-side drivers - the device's pullup, once it asserts, is
        what puts D+ in J state.

        The PLL behavioural model in `sim/ice40_pll_sim.v` is a
        passthrough, drive `clk16` at the *post-PLL* frequency
        (48 MHz) rather than 16 MHz"""
        cocotb.start_soon(Clock(self.pins.clk16, 20834, unit="ps").start())
        await self._release()
        await self._wait_pullup()

    async def _wait_pullup(self, timeout_us: int = 2000):
        """Poll usb_pullup until it goes high. Tolerates X while the
        design is still in reset / before the PLL lock model fires."""
        for _ in range(timeout_us):
            await Timer(1, unit="us")
            try:
                if int(self.pins.usb_pullup.value) == 1:
                    return
            except ValueError:
                continue  # X / Z - still settling
        raise TimeoutError("device did not assert usb_pullup")

    async def reset_bus(self, duration_us: int = 30):
        """Drive SE0 to issue a bus reset. The USB FS spec calls for
        ≥10 ms, but LUNA's `USBResetSequencer` accepts anything over
        2.5 µs. Use shorter time to help reduce sim times"""
        self.pins.usb_d_p.value = 0
        self.pins.usb_d_n.value = 0
        await Timer(duration_us, unit="us")
        await self._release()
        await Timer(5, unit="us")
        self.address = 0
        self.toggles.clear()
        self.max_packet0 = 8

    # ------------------------------------------------------------------
    # Line driver
    # ------------------------------------------------------------------

    async def _drive_levels(self, levels: list[int]):
        """Hold each `level` (0 = K, 1 = J) for one bit time."""
        for lvl in levels:
            if lvl == 1:    # J
                self.pins.usb_d_p.value = 1
                self.pins.usb_d_n.value = 0
            else:           # K
                self.pins.usb_d_p.value = 0
                self.pins.usb_d_n.value = 1
            await Timer(self.bit_ns, unit="ns")

    async def _drive_se0(self, bit_times: int = 2):
        self.pins.usb_d_p.value = 0
        self.pins.usb_d_n.value = 0
        await Timer(self.bit_ns * bit_times, unit="ns")

    async def _release(self):
        """Release the bus - host stops driving, device pullup wins."""
        release(self.pins.usb_d_p)
        release(self.pins.usb_d_n)

    # ------------------------------------------------------------------
    # Packet TX
    # ------------------------------------------------------------------

    async def send_packet(self, packet: bytes):
        """Serialise `packet` (PID + payload + CRC, no SYNC/EOP) to D+/D-."""
        framed = bytes([SYNC_BYTE]) + packet
        bits   = bytes_to_bits_lsb(framed)
        # The SYNC byte must NOT be bit-stuffed
        sync_bits, body_bits = bits[:8], bits[8:]
        stuffed = sync_bits + bit_stuff(body_bits)
        levels  = nrzi_encode(stuffed, start_level=1)
        await self._drive_levels(levels)
        await self._drive_se0()
        await self._release()

    # ------------------------------------------------------------------
    # Packet RX
    # ------------------------------------------------------------------

    async def receive_packet(self, timeout_us: int = 50) -> bytes | None:
        """Listen for the next packet the device sends. Returns the bytes
        from PID through CRC (no SYNC, no EOP), or None on timeout.

        Caller is expected to have just finished a host transmit and
        released the bus - we don't drive while sampling, otherwise the
        host's drive would collide with the device's on the inout pad."""
        await self._release()

        # Wait for the device to start driving K. Both D+ and D- read Z
        # while the bus is released (no one driving), so we need to look
        # for the device *actively* pulling D+ low and D- high - i.e. a
        # resolved, defined K state. Don't use `_level()` here: it maps
        # Z to 1 (so D+ idle reads 1 and D- floating reads 1 too, which
        # would look like K-state immediately after release).
        start_ns = 0
        while True:
            await Timer(1, unit="ns")
            start_ns += 1
            try:
                dp = int(self.pins.usb_d_p.value)
                dn = int(self.pins.usb_d_n.value)
            except (ValueError, TypeError):
                # Still floating - keep polling.
                if start_ns > timeout_us * 1000:
                    return None
                continue
            if dp == 0 and dn == 1:    # device driving K
                break
            if start_ns > timeout_us * 1000:
                return None

        # Anchor sampling to absolute target times to avoid the drift
        # an `await Timer(bit_ns)` loop would accumulate (83 ns is not
        # exactly one FS bit).
        first_k_ps = get_sim_time(unit="ps")
        levels = []
        i = 0
        while True:
            target = first_k_ps + self.half_bit_ps + i * self.bit_ps
            delay  = target - get_sim_time(unit="ps")
            if delay > 0:
                await Timer(delay, unit="ps")
            try:
                dp = int(self.pins.usb_d_p.value)
                dn = int(self.pins.usb_d_n.value)
            except (ValueError, TypeError):
                # Bus drifted to Z mid-packet - treat as EOP.
                break
            if dp == 0 and dn == 0:
                break  # SE0 - EOP
            levels.append(1 if dp == 1 else 0)
            i += 1

        # Inter-packet bus turnaround. The device finishes EOP with
        # ~3 bit times of SE0 + J + release, and we caught SE0 on its
        # very first sample. If the host immediately starts driving
        # the next packet, it collides with the tail of the device's
        # EOP. Wait long enough for the device to fully release
        # before returning. (USB FS spec: EOP is 2 bit times SE0 + 1
        # bit time J, then idle; receive-to-transmit interpacket
        # delay is at least 2 bit times.)
        await Timer(self.bit_ns * 5, unit="ns")

        bits     = nrzi_decode(levels, start_level=1)
        unstuffd = bit_unstuff(bits)
        raw      = bits_to_bytes_lsb(unstuffd)
        # First byte is SYNC; drop it.
        return raw[1:] if raw else b""

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def transaction_setup(self, addr: int, endpoint: int, payload: bytes):
        await self.send_packet(build_token(PID.SETUP, addr, endpoint))
        await Timer(self.bit_ns * 2, unit="ns")  # inter-packet delay
        await self.send_packet(build_data(PID.DATA0, payload))
        ack = await self.receive_packet()
        if not ack or ack[0] != PID.ACK.encoded():
            raise RuntimeError(f"SETUP not ACKed: got {ack!r}")

    async def transaction_in(self, addr: int, endpoint: int, expected_pid: PID | None = None) -> bytes:
        await self.send_packet(build_token(PID.IN, addr, endpoint))
        pkt = await self.receive_packet()
        if pkt is None:
            raise RuntimeError("IN: no response")
        pid = pkt[0]
        if pid == PID.NAK.encoded():
            return None  # caller retries
        if pid == PID.STALL.encoded():
            raise RuntimeError("endpoint stalled")
        if expected_pid is not None and pid != expected_pid.encoded():
            raise RuntimeError(f"IN: expected {expected_pid.name}, got pid={pid:02x}")
        payload, crc = pkt[1:-2], pkt[-2:]
        # TODO: verify CRC16(payload) == int.from_bytes(crc, "little")
        await Timer(self.bit_ns * 2, unit="ns")
        await self.send_packet(build_handshake(PID.ACK))
        return payload

    async def transaction_out(self, addr: int, endpoint: int, payload: bytes, *, data_pid: PID):
        await self.send_packet(build_token(PID.OUT, addr, endpoint))
        await Timer(self.bit_ns * 2, unit="ns")
        await self.send_packet(build_data(data_pid, payload))
        ack = await self.receive_packet()
        if not ack:
            raise RuntimeError("OUT: no handshake")
        if ack[0] == PID.NAK.encoded():
            return False
        if ack[0] != PID.ACK.encoded():
            raise RuntimeError(f"OUT: unexpected handshake {ack[0]:02x}")
        return True

    # ------------------------------------------------------------------
    # Control transfers
    # ------------------------------------------------------------------

    async def control_in(self, bm_request_type: int, b_request: int,
                         w_value: int = 0, w_index: int = 0,
                         w_length: int = 0) -> bytes:
        setup = bytes([
            bm_request_type, b_request,
            w_value & 0xFF, (w_value >> 8) & 0xFF,
            w_index & 0xFF, (w_index >> 8) & 0xFF,
            w_length & 0xFF, (w_length >> 8) & 0xFF,
        ])
        await self.transaction_setup(self.address, 0, setup)

        # Data stage - DATA1 first, then toggle each transaction.
        received = bytearray()
        toggle_pid = PID.DATA1
        remaining = w_length
        max_packet = self.max_packet0   # updated by `set_address`/descriptor reads
        while remaining > 0:
            payload = await self._retry_in(self.address, 0, expected_pid=toggle_pid)
            received.extend(payload)
            remaining -= len(payload)
            if len(payload) < max_packet:
                break
            toggle_pid = PID.DATA0 if toggle_pid is PID.DATA1 else PID.DATA1

        # Status stage - host issues zero-length OUT DATA1.
        await self.transaction_out(self.address, 0, b"", data_pid=PID.DATA1)
        return bytes(received)

    async def control_out(self, bm_request_type: int, b_request: int,
                          w_value: int = 0, w_index: int = 0,
                          data: bytes = b""):
        setup = bytes([
            bm_request_type, b_request,
            w_value & 0xFF, (w_value >> 8) & 0xFF,
            w_index & 0xFF, (w_index >> 8) & 0xFF,
            len(data) & 0xFF, (len(data) >> 8) & 0xFF,
        ])
        await self.transaction_setup(self.address, 0, setup)
        # Data stage - TODO: chunk per bMaxPacketSize0
        if data:
            await self.transaction_out(self.address, 0, data, data_pid=PID.DATA1)
        # Status stage - zero-length IN DATA1
        await self._retry_in(self.address, 0, expected_pid=PID.DATA1)

    async def _retry_in(self, addr, endpoint, *, expected_pid, max_retries: int = 10):
        for _ in range(max_retries):
            pkt = await self.transaction_in(addr, endpoint, expected_pid=expected_pid)
            if pkt is not None:
                return pkt
            await Timer(self.bit_ns * 4, unit="ns")
        raise RuntimeError("IN retries exhausted (device NAKing)")

    # ------------------------------------------------------------------
    # Standard requests
    # ------------------------------------------------------------------

    async def set_address(self, new_address: int):
        await self.control_out(0x00, 0x05, w_value=new_address)
        self.address = new_address

    async def get_descriptor(self, *, descriptor_type: int, index: int = 0,
                             lang_id: int = 0, length: int = 64) -> bytes:
        w_value = (descriptor_type << 8) | index
        data = await self.control_in(0x80, 0x06, w_value=w_value,
                                     w_index=lang_id, w_length=length)
        # Latch bMaxPacketSize0 the first time we see the device
        # descriptor so subsequent control transfers know when a short
        # packet means end-of-transfer.
        if descriptor_type == 0x01 and len(data) >= 8:
            self.max_packet0 = data[7]
        return data

    async def set_configuration(self, configuration: int):
        await self.control_out(0x00, 0x09, w_value=configuration)

    # ------------------------------------------------------------------
    # Bulk
    # ------------------------------------------------------------------

    async def bulk_out(self, endpoint: int, payload: bytes, *, max_packet: int = 64,
                       max_retries: int = 500, retry_interval_ns: int = 4000):
        """Send `payload` as bulk OUT transfers, retrying on NAK.
        """
        tog = self.toggles.setdefault(endpoint, EndpointToggle())
        for chunk_start in range(0, len(payload), max_packet):
            chunk = payload[chunk_start:chunk_start + max_packet]
            pid = PID.DATA1 if tog.out else PID.DATA0
            for _ in range(max_retries):
                ok = await self.transaction_out(self.address, endpoint, chunk, data_pid=pid)
                if ok:
                    break
                await Timer(retry_interval_ns, unit="ns")
            else:
                raise RuntimeError(
                    f"bulk_out: endpoint {endpoint} NAKed {max_retries} times "
                    f"at offset {chunk_start}"
                )
            tog.out ^= 1

    async def bulk_in(self, endpoint: int, length: int, *, max_packet: int = 64) -> bytes:
        tog = self.toggles.setdefault(endpoint, EndpointToggle())
        received = bytearray()
        while len(received) < length:
            expected = PID.DATA1 if tog.in_ else PID.DATA0
            chunk = await self._retry_in(self.address, endpoint, expected_pid=expected)
            received.extend(chunk)
            tog.in_ ^= 1
            if len(chunk) < max_packet:
                break
        return bytes(received)
