"""USB 2.0 high-speed host model for cocotb (ULPI based DUT).

`USBHostHS` reuses the entire transaction / control-transfer / bulk layer from
the full-speed :class:`~.usb_host.USBHost` - those methods only ever call
``send_packet`` / ``receive_packet`` and a couple of bus-idle helpers, which we
re-point here at the ULPI PHY model (`ulpi_phy.UlpiPhy`). The wire is now an
8-bit ULPI byte stream, so there's no SYNC / NRZI / bit-stuffing: the PHY model
frames packets and runs the high-speed chirp handshake.
"""

from __future__ import annotations

import cocotb
from cocotb.triggers import RisingEdge, Timer
from cocotb.clock import Clock

from .usb_host import USBHost
from .ulpi_phy import UlpiPhy
from .dut_pins import attach_hs


# 60 MHz ULPI / sync clock. cocotb drives the 25 MHz reference pad at the
# post-PLL rate because `ecp5_cells_sim.v`'s EHXPLLL is a passthrough.
# (Even ps so cocotb's Clock can split it 50/50.)
_CLK_PERIOD_PS = 16666


class USBHostHS(USBHost):
    """High-speed host: the FS transaction layer over a modelled ULPI PHY."""

    def __init__(self, dut):
        # Note: we intentionally don't call USBHost.__init__ (it attaches the
        # full-speed D+/D- pins). We set up the HS pins + PHY instead.
        self.dut = dut
        self.pins = attach_hs(dut)
        self.phy = UlpiPhy(self.pins)

        # Control EP0 max packet size (HS devices use 64); latched from the
        # device descriptor by get_descriptor().
        self.max_packet0 = 64
        self.address = 0
        self.toggles = {}

        # Inter-packet spacing. At HS the PHY model already inserts bus
        # turnaround; these short waits just give the SIE a cycle to react.
        self.bit_ns = 1

    # ------------------------------------------------------------------
    # Bring-up
    # ------------------------------------------------------------------

    async def start(self):
        """Start the 60 MHz clock and the PHY engine, then wait for the DUT to
        finish its boot read and assert `connect` before enumerating."""
        cocotb.start_soon(Clock(self.pins.clk, _CLK_PERIOD_PS, unit="ps").start())
        self.phy.start()
        await self._wait_connect()

    async def _wait_connect(self, timeout_us: int = 400):
        """Wait for the DUT's top FSM to reach USB-CONNECT (usb.connect high).
        Falls back to a fixed settle if the internal net isn't observable."""
        connect = getattr(self.dut, "connect", None)
        deadline = timeout_us * 100  # ~ticks at 60 MHz (0.6 us granularity)
        if connect is not None:
            for _ in range(deadline):
                await Timer(600, unit="ns")
                try:
                    if int(connect.value) & 1:
                        return
                except ValueError:
                    pass
            raise TimeoutError("DUT did not assert connect")
        # No observable connect net: just wait out a generous boot window.
        await Timer(timeout_us, unit="us")

    async def reset_bus(self, duration_us: int = 0):
        """Issue a high-speed bus reset: drive SE0, let the device chirp, then
        complete the host K/J chirp handshake so the link comes up at HS."""
        await self.phy.reset_and_chirp()
        self.address = 0
        self.toggles.clear()
        self.max_packet0 = 64

    # ------------------------------------------------------------------
    # Wire seam (overrides USBHost's D+/D- implementations)
    # ------------------------------------------------------------------

    async def send_packet(self, packet: bytes):
        await self.phy.send(packet)

    async def receive_packet(self, timeout_us: int = 50) -> bytes | None:
        # Convert the FS "us" budget into ULPI clock cycles (~60/us).
        return await self.phy.recv(timeout_cycles=max(64, timeout_us * 60))

    async def _drive_j_idle(self):
        # The PHY model maintains the idle line state; nothing to drive here.
        pass

    async def _release(self):
        pass

    async def _retry_in(self, addr, endpoint, *, expected_pid, max_retries=4000):
        """Poll an IN endpoint through NAKs. The HS CSW read in particular has
        to outwait the SCSI/UF2 flash program: at FS the write is throttled
        across many NAK-retried 64-byte OUT packets, but at HS the whole
        512-byte packet is buffered and ACKed at once, so the device NAKs every
        IN until it has drained the buffer to flash (~hundreds of microseconds).
        Give a generous budget with a real inter-poll delay."""
        for _ in range(max_retries):
            pkt = await self.transaction_in(addr, endpoint, expected_pid=expected_pid)
            if pkt is not None:
                return pkt
            await Timer(500, unit="ns")
        raise RuntimeError("IN retries exhausted (device NAKing)")
