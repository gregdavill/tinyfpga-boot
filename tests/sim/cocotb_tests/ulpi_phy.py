"""Behavioural USB3343 ULPI PHY model for cocotb.

What it implements (see luna/gateware/interface/ulpi.py + usb2/reset.py):

* Register writes - ACKs the function-control / OTG-control writes the
  `ULPIControlTranslator` issues at startup (else `busy` never clears).
* RX path - drives `dir`/`nxt`/`data` to deliver a host->device packet
  (token / DATA) as a byte stream, framed with an RxCmd that starts RxActive.
* TX path - captures a device->host packet: the first byte is a Transmit
  Command (`0x40 | PID[3:0]`); the rest are payload bytes up to `stp`.
* line-state / chirp - drives RxCmds reporting line state so the reset
  sequencer steps SE0-reset -> device chirp -> host K/J chirp -> HS idle.

Bus convention: the FPGA is the link, this model is the PHY. The model owns
`dir`/`nxt`, samples `stp`, and drives `data` only while `dir` is high. The
link drives `data` (and we sample it) while `dir` is low.

Half-duplex: the host serialises operations (send a token, then receive the
response), so the engine handles one bus operation at a time; between
operations it idles, ACKing register writes and capturing any device TX.
"""

from __future__ import annotations

import logging
import os
from collections import deque

import cocotb
from cocotb.triggers import RisingEdge, Timer, Event, First

from .usb_packets import PID

_log = logging.getLogger("ulpi_phy")
if os.environ.get("ULPI_DEBUG"):
    _log.setLevel(logging.DEBUG)


# ULPI command prefixes (top two bits of a TX-direction byte).
_CMD_MASK      = 0b11000000
_CMD_TRANSMIT  = 0b01000000
_CMD_REG_WRITE = 0b10000000
_CMD_REG_READ  = 0b11000000

# UTMI / ULPI line-state codes (match USBResetSequencer._LINE_STATE_*).
LS_SE0 = 0b00      # also HS squelch / reset
LS_J   = 0b01      # FS/HS J  (idle)
LS_K   = 0b10      # FS/HS K

# Cycles to hold each host chirp K/J. The reset sequencer requires the state
# to persist for _CYCLES_2P5_MICROSECONDS (= 150 at 60 MHz: 2.5 us) *after* it
# enters IN_HOST_K/J, plus a cycle to detect the edge, so hold comfortably
# past 150.
_CHIRP_HOLD = 180


def _rxcmd(line_state: int, rx_active: bool = False) -> int:
    """Build an RxCmd byte. bits[1:0]=line_state, [3:2]=VbusState (0b11 ->
    VbusValid, so the device sees vbus_connected), [4]=RxActive."""
    val = (line_state & 0b11) | (0b11 << 2)
    if rx_active:
        val |= (1 << 4)
    return val


class UlpiPhy:
    """Cocotb behavioural model of the USB3343 ULPI PHY."""

    # Settle delay after each clock edge before sampling/driving, so we read
    # the DUT's freshly-registered outputs and our drives meet next-edge setup.
    _SETTLE_PS = 2000

    def __init__(self, pins):
        self.p = pins
        # Captured device->host packets (PID byte + payload), popped by recv().
        self._rx_captured: deque[bytes] = deque()
        self._rx_event = Event()
        # Register writes the device performed: (address, value) - for asserts.
        self.reg_writes: list[tuple[int, int]] = []
        # line_state reported during HS idle (J avoids the 3 ms HS-suspend
        # timer, which counts SE0). Set once the link reaches high speed.
        self._idle_ls = LS_J
        # Serialised command slot the host fills; engine executes then signals.
        self._cmd = None
        self._cmd_done = Event()
        self._cmd_request = Event()

    # ------------------------------------------------------------------
    # Pin helpers
    # ------------------------------------------------------------------

    def _drive_data(self, val: int):
        self.p.ulpi_data.value = val & 0xFF

    def _release_data(self):
        self.p.ulpi_data.value = "zzzzzzzz"

    def _read_data(self) -> int:
        try:
            return int(self.p.ulpi_data.value) & 0xFF
        except ValueError:
            return 0  # x/z -> treat as NOP

    def _stp(self) -> int:
        try:
            return int(self.p.ulpi_stp.value) & 1
        except ValueError:
            return 0

    def _release_bus(self):
        """Idle: yield the data bus to the link, dir/nxt low."""
        self.p.ulpi_dir.value = 0
        self.p.ulpi_nxt.value = 0
        self._release_data()

    async def _tick(self):
        await RisingEdge(self.p.clk)
        await Timer(self._SETTLE_PS, "ps")

    # ------------------------------------------------------------------
    # Engine - the single owner of the ULPI bus.
    # ------------------------------------------------------------------

    def start(self):
        cocotb.start_soon(self._engine())

    async def _wait_reset_deassert(self, timeout_us: int = 200):
        """Wait for the link to release the ULPI reset (rst_invert: the pad is
        driven low while the design is held in reset)."""
        for _ in range(timeout_us * 100):
            await self._tick()
            try:
                if int(self.p.ulpi_rst.value) & 1:
                    return
            except ValueError:
                pass
        # Some builds may tie rst differently; don't hard-fail.

    async def _engine(self):
        self._release_bus()
        await self._wait_reset_deassert()
        while True:
            if self._cmd is not None:
                coro = self._cmd
                self._cmd = None
                await coro
                self._cmd_done.set()
                continue
            await self._service_idle_once()

    async def _service_idle_once(self):
        """One idle cycle: yield the bus to the link and service whatever it
        drives - register writes (ACK them) or a device transmit (capture it)."""
        self._release_bus()
        await self._tick()
        d = self._read_data()
        cmd = d & _CMD_MASK
        if cmd == _CMD_REG_WRITE:
            await self._ack_reg_write(d)
        elif cmd == _CMD_TRANSMIT:
            pkt = await self._capture_tx(d)
            if pkt is not None:
                self._rx_captured.append(pkt)
                self._rx_event.set()
        # else NOP / reg-read (device never reads) -> stay idle.

    async def _drain_idle(self, cycles: int):
        """Run idle servicing for `cycles`, so pending register writes (e.g.
        the function-control update after the device switches to high speed)
        complete before the next packet injection grabs the bus."""
        for _ in range(cycles):
            await self._service_idle_once()

    async def _run_command(self, coro):
        """Hand a bus operation to the engine and wait for it to finish."""
        self._cmd = coro
        self._cmd_done.clear()
        await self._cmd_done.wait()

    # ------------------------------------------------------------------
    # Register-write handshake
    # ------------------------------------------------------------------

    async def _ack_reg_write(self, first: int):
        # `first` (0x80|addr) is on the bus now (device in SEND_WRITE_ADDRESS).
        # Assert NXT to accept the command byte; next cycle holds write_data.
        self.p.ulpi_nxt.value = 1
        await self._tick()
        value = self._read_data()
        # Accept the data byte; the device then pulses STP.
        self.p.ulpi_nxt.value = 1
        await self._tick()
        self.p.ulpi_nxt.value = 0
        self.reg_writes.append((first & 0x3F, value))
        _log.debug("reg write: addr=0x%02x value=0x%02x", first & 0x3F, value)

    # ------------------------------------------------------------------
    # TX capture (device -> host)
    # ------------------------------------------------------------------

    async def _capture_tx(self, first: int, *, is_chirp: bool = False):
        """`first` = 0x40|PID[3:0] transmit command currently on the bus.
        Accept it and every following byte (asserting NXT) until STP, then
        return the reconstructed packet (full PID byte + payload)."""
        pid_nibble = first & 0x0F
        payload = bytearray()
        self.p.ulpi_nxt.value = 1   # accept command byte
        await self._tick()
        while not self._stp():
            payload.append(self._read_data())
            self.p.ulpi_nxt.value = 1
            await self._tick()
        self.p.ulpi_nxt.value = 0
        if is_chirp:
            _log.debug("captured chirp TX (%d bytes consumed)", len(payload))
            return None
        full_pid = pid_nibble | ((pid_nibble ^ 0xF) << 4)
        pkt = bytes([full_pid]) + bytes(payload)
        _log.debug("captured TX: %s", pkt.hex())
        return pkt

    # ------------------------------------------------------------------
    # RX drive (host -> device)
    # ------------------------------------------------------------------

    async def _drive_rx(self, packet: bytes):
        """Deliver `packet` (PID byte + payload + CRC) to the SIE over the RX
        path: turnaround, RxCmd starting RxActive, then one byte per cycle with
        NXT high, then an RxCmd clearing RxActive, then release the bus."""
        _log.debug("drive RX: %s", packet.hex())
        active = _rxcmd(self._idle_ls, rx_active=True)
        # Take the bus and present the RxActive RxCmd. The first cycle is the
        # bus turnaround (the decoder ignores it - it needs dir high >1 cycle);
        # hold the RxCmd a couple more cycles so the SIE's rx_active register
        # (RxCmd -> rx_start -> rx_active, ~2 cycles) is set before data flows,
        # otherwise the first byte (the PID) is dropped.
        self.p.ulpi_dir.value = 1
        self.p.ulpi_nxt.value = 0
        self._drive_data(active)
        await self._tick()   # turnaround (not sampled)
        await self._tick()   # RxCmd sampled -> rx_start
        await self._tick()   # rx_active settles
        # Payload bytes, NXT high (rx_valid = nxt & rx_active).
        for byte in packet:
            self.p.ulpi_nxt.value = 1
            self._drive_data(byte)
            await self._tick()
        # End: NXT low, RxCmd clearing RxActive -> rx_stop.
        self.p.ulpi_nxt.value = 0
        self._drive_data(_rxcmd(self._idle_ls, rx_active=False))
        await self._tick()
        # Release the bus (one turnaround cycle before the link may drive).
        self._release_bus()
        await self._tick()

    # ------------------------------------------------------------------
    # Reset + high-speed chirp handshake
    # ------------------------------------------------------------------

    async def _begin_drive(self):
        """Take the bus (dir high) with a turnaround cycle."""
        self.p.ulpi_dir.value = 1
        self.p.ulpi_nxt.value = 0
        self._drive_data(0)
        await self._tick()

    async def _drive_ls(self, line_state: int, cycles: int):
        """Hold an RxCmd reporting `line_state` for `cycles` (dir already high)."""
        self._drive_data(_rxcmd(line_state))
        self.p.ulpi_nxt.value = 0
        for _ in range(cycles):
            await self._tick()

    async def _await_chirp(self, timeout: int = 4000):
        """Wait for the device chirp (a transmit) and consume it to its STP.

        While waiting we must still service register writes: entering
        high-speed detection the device rewrites function-control
        (op_mode/term_select), and the chirp won't start until that write is
        acknowledged (PREPARE_FOR_CHIRP waits for ~bus_busy)."""
        for _ in range(timeout):
            self._release_bus()
            await self._tick()
            d = self._read_data()
            cmd = d & _CMD_MASK
            if cmd == _CMD_TRANSMIT:
                await self._capture_tx(d, is_chirp=True)
                return
            elif cmd == _CMD_REG_WRITE:
                await self._ack_reg_write(d)
        raise TimeoutError("device did not produce a high-speed chirp")

    async def _do_reset_chirp(self):
        # 1. Latch SE0 (+ VbusValid). The device counts SE0 and, once it sees
        #    >5 us, triggers a reset and enters high-speed detection.
        await self._begin_drive()
        await self._drive_ls(LS_SE0, cycles=1)
        self._release_bus()
        # 2. Device chirps K; wait for it to complete.
        await self._await_chirp()
        # 3. Host chirp: 3x (K, J), each held past the 2.5 us minimum.
        await self._begin_drive()
        for _ in range(3):
            await self._drive_ls(LS_K, _CHIRP_HOLD)
            await self._drive_ls(LS_J, _CHIRP_HOLD)
        # 4. High-speed idle: report J (non-SE0) so the HS-suspend timer
        #    (which counts SE0) never fires between transactions.
        self._idle_ls = LS_J
        await self._drive_ls(self._idle_ls, 2)
        self._release_bus()
        await self._tick()
        # Switching to high speed makes the device rewrite function-control
        # (op_mode -> NORMAL); service that register write now so it doesn't
        # block RxCmd sampling once we start injecting packets.
        await self._drain_idle(64)

    # ------------------------------------------------------------------
    # Host-facing API
    # ------------------------------------------------------------------

    async def reset_and_chirp(self):
        await self._run_command(self._do_reset_chirp())

    async def send(self, packet: bytes):
        await self._run_command(self._drive_rx(packet))

    async def recv(self, timeout_cycles: int = 4000) -> bytes | None:
        """Return the next device->host packet, or None on timeout."""
        waited = 0
        while not self._rx_captured:
            if waited >= timeout_cycles:
                return None
            self._rx_event.clear()
            # Wake on either a capture or a bounded number of cycles.
            timer = cocotb.start_soon(self._sleep_cycles(64))
            await First(self._rx_event.wait(), timer)
            waited += 64
        return self._rx_captured.popleft()

    async def _sleep_cycles(self, n: int):
        for _ in range(n):
            await RisingEdge(self.p.clk)
