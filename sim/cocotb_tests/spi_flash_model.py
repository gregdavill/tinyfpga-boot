"""Behavioural QSPI flash model for cocotb.

Implements only the opcodes the bootloader issues:

* 0x4B  Read Unique ID Number     (sent by `FlashUID` on boot)
* 0x06  Write Enable              (UF2 write path)
* 0x05  Read Status Register      (poll WIP)
* 0x02  Page Program (1-1-1)
* 0x32  Quad Page Program         (1-1-4)
* 0x20  Sector Erase (4 KiB)
* 0x6B  Fast Read Quad Output     (1-1-4) - used by some boot ROMs
* 0x0B  Fast Read                  (1-1-1)
* 0xAB  Release Power-down / Read Device ID

The model captures every command into `self.transactions` so tests can
assert on the exact sequence of opcodes + payloads the DUT issued.

It also exposes a `memory` bytearray that tests can preload (e.g. to
plant a UID) or inspect (to verify a program landed in the right
offset).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import cocotb
from cocotb.triggers import Edge, RisingEdge, FallingEdge, First, Timer, Event

from .dut_pins import attach as attach_pins, release
from . import coverage as _cov


def _read_dq(signal, *, lanes: int) -> int:
    """Sample the low `lanes` bits of a multi-bit inout pad. Returns
    the integer value of those bits, treating Z / X on the upper lanes
    as don't-care (they may be undriven when the DUT is in 1-lane or
    2-lane mode). Plain `int(signal.value)` raises on Z/X anywhere in
    the bus."""
    val = signal.value
    result = 0
    for i in range(lanes):
        try:
            b = int(val[i])
        except (ValueError, TypeError):
            b = 0
        result |= (b & 1) << i
    return result


class _Phase(enum.Enum):
    IDLE      = enum.auto()
    OPCODE    = enum.auto()
    ADDRESS   = enum.auto()
    DUMMY     = enum.auto()
    READ      = enum.auto()
    WRITE     = enum.auto()


@dataclass
class FlashTransaction:
    opcode: int
    address: int | None = None
    write_data: bytes = b""
    read_data:  bytes = b""

    def __repr__(self):  # pragma: no cover - debugging
        bits = [f"op={self.opcode:#04x}"]
        if self.address is not None:
            bits.append(f"addr={self.address:#08x}")
        if self.write_data:
            bits.append(f"wr={len(self.write_data)}B")
        if self.read_data:
            bits.append(f"rd={len(self.read_data)}B")
        return "FlashTx(" + ", ".join(bits) + ")"


class SPIFlashModel:
    """Subset of a Winbond W25Qxx-style serial flash.

    Wire-up: instantiate, then `cocotb.start_soon(model.run())`.
    Drives `spi_dq_i` when the DUT is reading; samples `spi_dq_o`
    otherwise. Listens for CS# transitions to frame commands.
    """

    PAGE_SIZE   = 256
    SECTOR_SIZE = 4096

    def __init__(self, dut, *, size: int = 16 * 1024 * 1024,
                 uid: bytes = b"\xCA\xFE\xBA\xBE\xDE\xAD\xBE\xEF"):
        assert len(uid) == 8
        self.dut    = dut
        self.pins   = attach_pins(dut)
        self.size   = size
        self.memory = bytearray(b"\xFF" * size)
        self.uid    = uid
        self._cs_idle   = 1
        self._cs_active = 0
        self.transactions: list[FlashTransaction] = []
        # Status register: bit0 = WIP (write-in-progress), bit1 = WEL.
        self.status = 0x00
        # Release DQ - the DUT drives it during command/address; the
        # model drives it during read phases.
        release(self.pins.spi_dq)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        """Wait for CS# falling edges and handle each as a transaction."""
        while True:
            await FallingEdge(self.pins.spi_cs)
            await self._handle_transaction()

    async def _wait_cs_idle(self):
        """Block until CS# rises (deassertion)."""
        await RisingEdge(self.pins.spi_cs)

    async def _handle_transaction(self):
        """One CS-framed transaction. Returns when CS goes back to its
        idle level (i.e., the DUT deasserts the chip select)."""
        tx = FlashTransaction(opcode=0)
        cs_idle = cocotb.start_soon(self._wait_cs_idle())

        # --- opcode (1-bit) ---
        opcode_task = cocotb.start_soon(self._shift_in(8, lanes=1))
        await First(opcode_task, cs_idle)
        if not opcode_task.done():
            # CS rose mid-opcode — aborted transaction.
            opcode_task.kill()
            return
        tx.opcode = opcode_task.result()
        _cov.cover_flash_opcode(tx.opcode)

        # Dispatch on opcode. Each handler awaits `cs_idle` either
        # directly (for fixed-length commands like Read UID) or via a
        # `First()` race against a streaming task (for commands the
        # host can stretch arbitrarily).
        try:
            if tx.opcode == 0x4B:
                await self._cmd_read_uid(tx, cs_idle)
            elif tx.opcode == 0x06:
                self.status |= 0x02        # WEL
                tx.read_data = b""
            elif tx.opcode == 0x04:
                self.status &= ~0x02
            elif tx.opcode == 0x05:
                await self._cmd_read_status(tx, cs_idle)
            elif tx.opcode == 0x02:
                await self._cmd_page_program(tx, cs_idle, lanes=1)
            elif tx.opcode == 0x32:
                await self._cmd_page_program(tx, cs_idle, lanes=4)
            elif tx.opcode == 0x20:
                await self._cmd_sector_erase(tx, cs_idle)
            elif tx.opcode == 0x0B:
                await self._cmd_fast_read(tx, cs_idle, lanes=1)
            elif tx.opcode == 0x6B:
                await self._cmd_fast_read(tx, cs_idle, lanes=4)
            elif tx.opcode == 0xAB:
                await self._cmd_release_pd(tx, cs_idle)
            else:
                tx.read_data = b"<unhandled>"
                await cs_idle
        finally:
            self.transactions.append(tx)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_read_uid(self, tx: FlashTransaction, cs_idle):
        # 0x4B [4 dummy bytes] [8 UID bytes out]
        for _ in range(4):
            await self._shift_in(8, lanes=1)
        await self._shift_out(self.uid, lanes=1)
        tx.read_data = self.uid
        await cs_idle

    async def _cmd_read_status(self, tx: FlashTransaction, cs_idle):
        # Drive status until CS# rises (host may clock as many bytes as it wants).
        async def stream():
            while True:
                await self._shift_out(bytes([self.status]), lanes=1)
        task = cocotb.start_soon(stream())
        await cs_idle
        task.kill()
        tx.read_data = bytes([self.status])

    async def _cmd_page_program(self, tx: FlashTransaction, cs_idle, *, lanes: int):
        addr_bytes = await self._shift_in_bytes(3, lanes=1)
        addr = (addr_bytes[0] << 16) | (addr_bytes[1] << 8) | addr_bytes[2]
        tx.address = addr
        # Pull data until CS# rises.
        data = bytearray()

        async def stream():
            while True:
                byte = await self._shift_in(8, lanes=lanes)
                data.append(byte)

        task = cocotb.start_soon(stream())
        await cs_idle
        task.kill()
        tx.write_data = bytes(data)
        if self.status & 0x02:  # WEL must be set
            self._apply_page_program(addr, data)
            self.status &= ~0x02

    async def _cmd_sector_erase(self, tx: FlashTransaction, cs_idle):
        addr_bytes = await self._shift_in_bytes(3, lanes=1)
        addr = (addr_bytes[0] << 16) | (addr_bytes[1] << 8) | addr_bytes[2]
        tx.address = addr
        await cs_idle
        if self.status & 0x02:
            base = addr & ~(self.SECTOR_SIZE - 1)
            self.memory[base:base + self.SECTOR_SIZE] = b"\xFF" * self.SECTOR_SIZE
            self.status &= ~0x02

    async def _cmd_fast_read(self, tx: FlashTransaction, cs_idle, *, lanes: int):
        addr_bytes = await self._shift_in_bytes(3, lanes=1)
        addr = (addr_bytes[0] << 16) | (addr_bytes[1] << 8) | addr_bytes[2]
        tx.address = addr
        # 8 dummy cycles regardless of lanes - count as 1 single-lane byte
        # for 1-1-1, or 2 quad-lane bytes for 1-1-4. We approximate.
        await self._shift_in(8 if lanes == 1 else 2, lanes=1)

        out = bytearray()

        async def stream():
            offset = addr
            while True:
                byte = self.memory[offset % self.size]
                await self._shift_out(bytes([byte]), lanes=lanes)
                out.append(byte)
                offset += 1

        task = cocotb.start_soon(stream())
        await cs_idle
        task.kill()
        tx.read_data = bytes(out)

    async def _cmd_release_pd(self, tx: FlashTransaction, cs_idle):
        # Reply with one ID byte if the host clocks one out.
        await self._shift_in(24, lanes=1)
        await self._shift_out(b"\x14", lanes=1)
        await cs_idle

    # ------------------------------------------------------------------
    # Memory primitives
    # ------------------------------------------------------------------

    def _apply_page_program(self, addr: int, data: bytes):
        # Real flash AND-s; here we mimic that: only bits going from 1
        # to 0 are allowed.
        for i, byte in enumerate(data):
            page_offset = (addr & (self.PAGE_SIZE - 1)) + i
            wrap = page_offset % self.PAGE_SIZE
            phys = (addr & ~(self.PAGE_SIZE - 1)) + wrap
            self.memory[phys] &= byte

    # ------------------------------------------------------------------
    # SPI shifters
    # ------------------------------------------------------------------

    async def _shift_in(self, bits: int, *, lanes: int) -> int:
        """Capture `bits` bits across `lanes` DQ lines, MSB-first within
        each clock cycle. Returns the integer value."""
        assert bits % lanes == 0
        _cov.cover_qspi_lanes(lanes)
        cycles = bits // lanes
        value  = 0
        # Make sure we're not driving DQ - the DUT is.
        release(self.pins.spi_dq)
        for _ in range(cycles):
            await RisingEdge(self.pins.spi_clk)
            sample = _read_dq(self.pins.spi_dq, lanes=lanes)
            value = (value << lanes) | sample
        return value

    async def _shift_in_bytes(self, n: int, *, lanes: int) -> bytes:
        return bytes([await self._shift_in(8, lanes=lanes) for _ in range(n)])

    async def _shift_out(self, data: bytes, *, lanes: int):
        """Drive `data` MSB-first onto DQ over `lanes` lines. Driven on
        the falling edge so the DUT samples cleanly on the next rising."""
        _cov.cover_qspi_lanes(lanes)
        mask = (1 << lanes) - 1
        for byte in data:
            cycles = 8 // lanes
            for c in range(cycles):
                shift = 8 - lanes * (c + 1)
                nibble = (byte >> shift) & mask
                await FallingEdge(self.pins.spi_clk)
                # Preserve the upper DQ lanes (they may be inputs for the
                # DUT during 1-lane reads - leave them hi-Z).
                if lanes == 4:
                    self.pins.spi_dq.value = nibble
                else:
                    # 1-lane: drive DQ1 (MISO), leave DQ0 hi-Z.
                    self.pins.spi_dq.value = (nibble << 1) | 0b0000
