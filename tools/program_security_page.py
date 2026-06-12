#!/usr/bin/env python3
"""Program a flash security register with the bootloader's UUID.

The bootloader can read iSerialNumber from a small JSON blob stored in
the flash *security register* (read with opcode 0x48). This tool writes that
blob using a **vendor EP0 SPI bridge** over USB.

The blob matches the gateware's JsonStringKeyParser. Gateware just scans for "uuid" as its
USB serial descriptior value.

    {"boardmeta": {"name": "<board>", "fpga": "<part>", "hver": "<ver>",
                   "uuid": "<uuid>"}}

Security registers are each 256 bytes (rewritable until locked), but address differs by vendor.
The shift is auto-selected from the JEDEC manufacturer ID. override with --addr-offset-bits.

    Adesto/Renesas (e.g. AT25SF081): offset 0 -> 0x000100 / 0x000200 / 0x000300
    Winbond        (e.g. W25Q128):   offset 4 -> 0x001000 / 0x002000 / 0x003000

```
python tools/program_security_page.py --name "TinyFPGA BX" --fpga ice40lp8k-cm81 --hver 1.0.0
python tools/program_security_page.py --read
```
"""

import argparse
import json
import sys
import time

import usb.core
import uuid6


# --- security-register command set (shared by Adesto AT25SF0x / Winbond W25Qxx)
WREN      = 0x06
RDSR1     = 0x05
RDID      = 0x9F
SEC_READ  = 0x48
SEC_PROG  = 0x42
SEC_ERASE = 0x44

WIP_BIT    = 0x01
SEC_REG_SIZE = 256

# Security-register address shift keyed by JEDEC manufacturer ID (first byte).
# Matches the gateware's per-board `security_page_addr_offset_bits`.
SEC_ADDR_OFFSET_BITS = {
    0x1F: 0,   # Adesto / Renesas (e.g. AT25SF081) -> 0x100 / 0x200 / 0x300
    0xEF: 4,   # Winbond          (e.g. W25Q128)   -> 0x1000 / 0x2000 / 0x3000
}
DEFAULT_OFFSET_BITS = 0

DEFAULT_VID = 0x1209   # UF2 / DFU personalities present these IDs
DEFAULT_PID = 0x5af0


def reg_addr(n, offset_bits):
    if n not in (1, 2, 3):
        raise SystemExit("register must be 1, 2 or 3")
    return n << (8 + offset_bits)


def addr3(a):
    return [(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF]


def parse_meta_fields(tokens):
    """Collect leftover ``--key value`` / ``--key=value`` CLI tokens into a dict
    of board-metadata fields."""
    meta = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            raise SystemExit(f"unexpected argument: {tok!r}")
        key = tok[2:]
        if "=" in key:
            key, val = key.split("=", 1)
            i += 1
        elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            val = tokens[i + 1]
            i += 2
        else:
            raise SystemExit(f"option --{key} expects a value")
        meta[key] = val
    return meta


class VendorSpiBridge:
    """USB transport to the bootloader's vendor EP0 SPI bridge."""

    RT_EXEC    = 0x40      # host->device | vendor | device
    RT_RESULT  = 0xC0      # device->host | vendor | device
    REQ_EXEC   = 0x01
    REQ_RESULT = 0x02

    def __init__(self, dev):
        self._dev = dev

    @classmethod
    def open(cls, vid, pid):
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise SystemExit(f"no USB device {vid:#06x}:{pid:#06x} found "
                             f"(is the bootloader enumerated?)")
        return cls(dev)

    def transfer(self, write_bytes, read_len=0):
        """Clock out `write_bytes`; return `read_len` bytes read back."""
        self._dev.ctrl_transfer(self.RT_EXEC, self.REQ_EXEC,
                                read_len, 0, bytes(write_bytes))
        if read_len:
            return bytes(self._dev.ctrl_transfer(self.RT_RESULT, self.REQ_RESULT,
                                                 0, 0, read_len))
        return b""


class Flash:
    """Security-register SPI commands (Adesto/Winbond compatible)."""

    def __init__(self, spi):
        self._spi = spi

    def jedec(self):
        return tuple(self._spi.transfer([RDID], 3))

    def status(self):
        return self._spi.transfer([RDSR1], 1)[0]

    def _wait(self, timeout=2.0):
        t0 = time.monotonic()
        while self.status() & WIP_BIT:
            if time.monotonic() - t0 > timeout:
                raise SystemExit("timed out waiting for WIP to clear")
            time.sleep(0.001)

    def read_sec(self, addr, n):
        # opcode + 3 address bytes + 1 dummy byte, then read n bytes.
        return self._spi.transfer([SEC_READ] + addr3(addr) + [0x00], n)

    def erase_sec(self, addr):
        self._spi.transfer([WREN])
        self._spi.transfer([SEC_ERASE] + addr3(addr))
        self._wait()

    def prog_sec(self, addr, data):
        if len(data) > SEC_REG_SIZE:
            raise SystemExit(f"payload {len(data)} B exceeds the "
                             f"{SEC_REG_SIZE}-byte security register")
        self._spi.transfer([WREN])
        self._spi.transfer([SEC_PROG] + addr3(addr) + list(data))
        self._wait()


def _dump(data):
    text = bytes(b if 0x20 <= b < 0x7F else 0x2E for b in data).decode("ascii")
    print(f"  ascii: {text}")
    print(f"  hex  : {data[:64].hex()}{'...' if len(data) > 64 else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vid", type=lambda x: int(x, 0), default=DEFAULT_VID,
                    help="USB idVendor (default: 0x1209)")
    ap.add_argument("--pid", type=lambda x: int(x, 0), default=DEFAULT_PID,
                    help="USB idProduct (default: 0x5af0)")
    ap.add_argument("--register", type=int, default=1, choices=(1, 2, 3),
                    help="security register to use (default: 1)")
    ap.add_argument("--addr-offset-bits", type=int, default=None,
                    help="security-register address shift (default: auto from "
                         "JEDEC ID; Adesto=0 -> 0x100, Winbond=4 -> 0x1000)")
    ap.add_argument("--uuid", default=None,
                    help="UUID string to store (default: a fresh uuid7)")
    ap.add_argument("--read", action="store_true",
                    help="just read and print the register, don't write")
    ap.add_argument("--no-erase", action="store_true",
                    help="skip the erase step (only if the register is blank)")
    ap.add_argument("--yes", action="store_true",
                    help="don't prompt before erasing/programming")
    # Any other `--key value` becomes a boardmeta field, e.g.
    #   --name "TinyFPGA BX" --fpga ice40lp8k-cm81 --hver 1.0.0
    args, extra = ap.parse_known_args()

    flash = Flash(VendorSpiBridge.open(args.vid, args.pid))

    jid = flash.jedec()
    print(f"JEDEC ID: {' '.join(f'{b:02X}' for b in jid)}")

    if args.addr_offset_bits is not None:
        offset_bits = args.addr_offset_bits
    else:
        offset_bits = SEC_ADDR_OFFSET_BITS.get(jid[0], DEFAULT_OFFSET_BITS)
    addr = reg_addr(args.register, offset_bits)

    if args.read:
        print(f"security register #{args.register} @ 0x{addr:06X}:")
        _dump(flash.read_sec(addr, SEC_REG_SIZE))
        return

    uuid_str = (args.uuid or str(uuid6.uuid7())).upper()
    if len(uuid_str) > 36:
        raise SystemExit("uuid longer than 36 chars (gateware descriptor max_len)")

    # Only `uuid` is required; any other `--key value` is optional metadata.
    meta = parse_meta_fields(extra)
    meta["uuid"] = uuid_str

    blob = json.dumps({"boardmeta": meta}).encode()
    if len(blob) > SEC_REG_SIZE:
        raise SystemExit(f"payload {len(blob)} B exceeds the security register")
    print(f"payload ({len(blob)} B): {blob.decode()}")

    if not args.yes:
        ans = input(f"erase + program security register #{args.register} "
                    f"(0x{addr:06X})? [y/N] ").strip().lower()
        if ans != "y":
            raise SystemExit("aborted")

    if not args.no_erase:
        flash.erase_sec(addr)
        print("erased")
    flash.prog_sec(addr, blob)
    print("programmed")

    got = flash.read_sec(addr, len(blob))
    if got != blob:
        print("VERIFY FAILED", file=sys.stderr)
        print(f"  wrote: {blob}", file=sys.stderr)
        print(f"  read : {got}", file=sys.stderr)
        sys.exit(1)
    print("verify OK")


if __name__ == "__main__":
    main()
