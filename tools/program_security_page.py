#!/usr/bin/env python3
"""Program a W25Q128 security register with bootloader's UUID.

The bootloader can read iSerialNumber from a small JSON blob stored in
the flash *security register* (read with opcode 0x48). This tool writes that
blob using a **vendor EP0 SPI bridge** over USB.

The blob matches the gateware's JsonStringKeyParser:

    {"boardmeta": {"name": "<board>", "uuid": "<uuid>"}}

W25Q128 security registers (each 256 bytes, rewritable until locked):

    register 1 -> flash address 0x001000
    register 2 -> flash address 0x002000
    register 3 -> flash address 0x003000

```
python tools/program_security_page.py --name "OrangeCrab r0.2"
python tools/program_security_page.py --read
```
"""

import argparse
import json
import sys
import time

import usb.core
import uuid6


# --- W25Q128 command set
WREN      = 0x06
RDSR1     = 0x05
RDID      = 0x9F
SEC_READ  = 0x48
SEC_PROG  = 0x42
SEC_ERASE = 0x44

WIP_BIT    = 0x01
W25Q128_ID = (0xEF, 0x40, 0x18)
SEC_REG_SIZE = 256

DEFAULT_VID = 0x1209   # UF2 / DFU personalities present these IDs
DEFAULT_PID = 0x5af0


def reg_addr(n):
    if n not in (1, 2, 3):
        raise SystemExit("register must be 1, 2 or 3")
    return n << 12                     # 0x1000 / 0x2000 / 0x3000


def addr3(a):
    return [(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF]


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
    """W25Q128 SPI commands"""

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
                    help="security register to use (default: 1 -> 0x001000)")
    ap.add_argument("--uuid", default=None,
                    help="UUID string to store (default: a fresh uuid7)")
    ap.add_argument("--name", default="OrangeCrab r0.2",
                    help="board name stored alongside the uuid")
    ap.add_argument("--read", action="store_true",
                    help="just read and print the register, don't write")
    ap.add_argument("--no-erase", action="store_true",
                    help="skip the erase step (only if the register is blank)")
    ap.add_argument("--yes", action="store_true",
                    help="don't prompt before erasing/programming")
    args = ap.parse_args()

    flash = Flash(VendorSpiBridge.open(args.vid, args.pid))

    jid = flash.jedec()
    print(f"JEDEC ID: {' '.join(f'{b:02X}' for b in jid)}")
    addr = reg_addr(args.register)

    if args.read:
        print(f"security register #{args.register} @ 0x{addr:06X}:")
        _dump(flash.read_sec(addr, SEC_REG_SIZE))
        return

    uuid_str = (args.uuid or str(uuid6.uuid7())).upper()
    if len(uuid_str) > 36:
        raise SystemExit("uuid longer than 36 chars (gateware descriptor max_len)")

    blob = json.dumps({"boardmeta": {"name": args.name, "uuid": uuid_str}}).encode()
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
