"""Pluggable USB personalities/backends

A backend supplies:
  * its USB IDs and device class (for the device descriptor),
  * the configuration descriptor's interface(s) and endpoints,
  * any class control-request handlers,
  * the wrapped bulk/interrupt endpoints to add to the device, and
  * its datapath, registered in `build()`, exposing QSPI-facing `qo`/`qi`
    streams that `Top` wires to the shared controller inside `USB-CONNECT`.

`build()` returns a `Reconfig(arm, activity)` the platform's reconfigure
primitive consumes.
"""

from collections import namedtuple

from amaranth import Signal, ResetInserter
from amaranth.lib import enum, wiring

from blocks.flash import QspiFlash, FlashPort
from blocks.dual_bank_writer import DualBankWriter
from blocks.arbiter import Arbiter
from tools.ecp_bitstream import flash_header

from config import Backend as BackendKind


Reconfig = namedtuple("Reconfig", ["arm", "activity"])


class Status(enum.Enum, shape=2):
    """Coarse backend activity, for board status indicator
    """
    IDLE   = 0  #: enumerated, waiting for a transfer
    ACTIVE = 1  #: transfer in progress
    DONE   = 2  #: transfer complete, reload pending
    ERROR  = 3  #: protocol/decode fault


class UsbAlloc:
    """Hands out unique USB interface and endpoint numbers.

    A single backend gets a fresh allocator (interface 0, endpoint 1).
    A composite device shares one allocator across its children so their 
    interfaces/endpoints never collide.
    """
    def __init__(self):
        self._if = 0
        self._ep = 1

    def interface(self):
        n = self._if
        self._if += 1
        return n

    def endpoint(self):
        n = self._ep
        self._ep += 1
        return n


class Backend:
    #: (idVendor, idProduct) presented in the device descriptor.
    usb_ids = (0x1209, 0x5af0)
    #: (bDeviceClass, bDeviceSubclass, bDeviceProtocol).
    device_class = (0, 0, 0)
    #: USB descriptor iProduct becomes "<board model> (<personality>)".
    personality = "Bootloader"
    #: Dual-bank `cfg_ctrl` default (0 = FLASH)
    cfg_ctrl_o = 0
    #: True when this backend writes an image to flash.
    writes_flash = False

    def __init__(self, config, *, hs, alloc=None):
        self.config = config
        self.hs = hs
        self.alloc = alloc if alloc is not None else UsbAlloc()
        #: QSPI-facing streams, populated by `build()`.
        self.qo = None
        self.qi = None
        #: activity state
        self.status = Signal(Status)

    def populate_configuration(self, c, *, bulk_mps):
        """Add this backend's interface + endpoint descriptors to a
        configuration descriptor (shared by the active and other-speed
        configurations)."""
        raise NotImplementedError

    def request_handlers(self):
        """Class control-request handlers to add to the control endpoint."""
        return []

    def msft_compat_ids(self):
        """`(bFirstInterfaceNumber, compatibleID)` pairs for interfaces that
        need a Windows MS OS 1.0 compatible-ID (e.g. "WINUSB")."""
        return []

    def endpoints(self):
        """Wrapped LUNA endpoints to add to the device."""
        return []

    def build(self, m, *, usb):
        """Register the datapath submodules and wiring and return
        `Reconfig(arm, activity)`."""
        raise NotImplementedError


def create_backing(m, *, config, reset, name="backing"):
    if config.has_ram_bank:
        backing = DualBankWriter(
            header_bytes=flash_header(), base_addr=config.reload_image_offset)
        m.submodules[name] = backing
        m.d.comb += backing.clear.eq(reset)
    else:
        backing = ResetInserter(reset)(QspiFlash())
        m.submodules[name] = backing
    return backing


def bind_writers(m, *, backing, writers):
    """Connect one or more `writes_flash` backends to `backing`."""
    if len(writers) == 1:
        wiring.connect(m, writers[0].wr, backing.port)
    else:
        m.submodules.wr_arb = arb = Arbiter(FlashPort, len(writers))
        for i, wtr in enumerate(writers):
            wiring.connect(m, wtr.wr, arb.inp[i])
            m.d.comb += arb.active[i].eq(wtr.wr.w.valid | wtr.wr.flush)
        wiring.connect(m, arb.out, backing.port)

    for wtr in writers:
        m.d.comb += [
            wtr.writing.eq(backing.active),
            wtr.arm_in.eq(backing.arm),
        ]
        wtr.cfg_ctrl_o = backing.cfg_ctrl_o


from .uf2_msc import Uf2MscBackend              # noqa: E402
from .serial_bridge import SerialBridgeBackend  # noqa: E402
from .dfu import DfuBackend                      # noqa: E402

BACKENDS = {
    BackendKind.UF2_MSC:         Uf2MscBackend,
    BackendKind.TINYFPGA_SERIAL: SerialBridgeBackend,
    BackendKind.DFU:             DfuBackend,
}
