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

from config import Backend as BackendKind


Reconfig = namedtuple("Reconfig", ["arm", "activity"])


class Backend:
    #: (idVendor, idProduct) presented in the device descriptor.
    usb_ids = (0x1209, 0x5af0)
    #: (bDeviceClass, bDeviceSubclass, bDeviceProtocol).
    device_class = (0, 0, 0)

    def __init__(self, config, *, hs):
        self.config = config
        self.hs = hs
        #: QSPI-facing streams, populated by `build()`.
        self.qo = None
        self.qi = None

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
