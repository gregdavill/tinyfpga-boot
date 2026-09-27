"""Speed-independent USB packet primitives shared by the FS and HS hosts.

These are the bits of the host model that don't care whether the wire is
full-speed D+/D- NRZI (`usb_host.py`) or a high-speed ULPI byte stream
(`usb_host_hs.py`): PID encoding, the token/data CRCs, packet assembly, and
the per-endpoint data-toggle bookkeeping.

Wire-level encoding (SYNC, NRZI, bit-stuffing) lives in `usb_host.py` because
it only applies to the FS gateware-PHY path; on ULPI the external PHY does
all of that.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


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
# Packet assembly (PID + payload + CRC; no SYNC/EOP, no NRZI/bit-stuffing)
# ----------------------------------------------------------------------

def build_token(pid: PID, address: int, endpoint: int) -> bytes:
    word = (address & 0x7F) | ((endpoint & 0xF) << 7)
    word |= (crc5(word, bits=11) & 0x1F) << 11
    return bytes([pid.encoded(), word & 0xFF, (word >> 8) & 0xFF])


def build_sof(frame_number: int) -> bytes:
    """SOF token: PID + 11-bit frame number + CRC5."""
    word = frame_number & 0x7FF
    word |= (crc5(word, bits=11) & 0x1F) << 11
    return bytes([PID.SOF.encoded(), word & 0xFF, (word >> 8) & 0xFF])


def build_data(pid: PID, payload: bytes) -> bytes:
    crc = crc16(payload)
    return bytes([pid.encoded()]) + payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_handshake(pid: PID) -> bytes:
    return bytes([pid.encoded()])


# ----------------------------------------------------------------------
# Endpoint state
# ----------------------------------------------------------------------

@dataclass
class EndpointToggle:
    out: int = 0
    in_:  int = 0
