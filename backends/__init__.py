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

from amaranth import Signal
from amaranth.lib import enum

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

    def endpoints(self):
        """Wrapped LUNA endpoints to add to the device."""
        return []

    def build(self, m, *, usb):
        """Register the datapath submodules and wiring, set `self.qo`/`self.qi`,
        and return `Reconfig(arm, activity)`."""
        raise NotImplementedError


from .uf2_msc import Uf2MscBackend              # noqa: E402
from .serial_bridge import SerialBridgeBackend  # noqa: E402
from .dfu import DfuBackend                      # noqa: E402

BACKENDS = {
    BackendKind.UF2_MSC:         Uf2MscBackend,
    BackendKind.TINYFPGA_SERIAL: SerialBridgeBackend,
    BackendKind.DFU:             DfuBackend,
}
