"""`CocotbPlatform` - `TinyFPGABXPlatform` with the toolchain skipped.

This keeps iCE40-specific primitives. cocotb picks up verilog behavial 
models.

`request()` is subclassed to handle the QSPI controller's ask for 
dir='-'. Capture these ports for inclusion in the top-level Verilog
port list.
"""

from amaranth.build.res import PortGroup
from amaranth.lib import io
from config.tinyfpga_bx import TinyFPGABXPlatform


class CocotbPlatform(TinyFPGABXPlatform):
    """Sim-friendly view of the TinyFPGA BX platform (inherits the project
    platform's clock/reset generation)."""

    toolchain = None

    def __init__(self):
        super().__init__()
        # Raw `dir="-"` ports recorded so `elaborate.py` can list them
        # as top-level Verilog ports.
        self._raw_ports: list = []

    def request(self, name, number=0, *, dir=None, xdr=None):
        result = super().request(name, number, dir=dir, xdr=xdr)
        if dir == "-":
            self._collect_raw(result)
        return result

    def _collect_raw(self, value):
        if isinstance(value, io.SingleEndedPort):
            self._raw_ports.append(value.io)
        elif isinstance(value, io.DifferentialPort):
            self._raw_ports.extend([value.p, value.n])
        elif isinstance(value, PortGroup):
            for sub in vars(value).values():
                self._collect_raw(sub)
