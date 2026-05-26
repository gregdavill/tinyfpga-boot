"""`CocotbPlatform` - `TinyFPGABXPlatform` with the toolchain skipped.

This keeps iCE40-specific primitives. cocotb picks up verilog behavial
models.

`request()` is subclassed to handle the QSPI controller's ask for
dir='-'. Capture these ports for inclusion in the top-level Verilog
port list.
"""

from amaranth.build import Resource, PinsN, Attrs
from amaranth.build.res import PortGroup
from amaranth.lib import io
from config.tinyfpga_bx import TinyFPGABXPlatform


class _RawPortCaptureMixin:
    """Records `dir="-"` ports (e.g. the QSPI controller's raw flash bus) so
    `elaborate.py` can surface them as top-level Verilog ports. Mix in *before*
    the concrete amaranth platform so `super().request()` reaches it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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


class CocotbPlatform(_RawPortCaptureMixin, TinyFPGABXPlatform):
    """Sim-friendly view of the TinyFPGA BX platform (inherits the project
    platform's clock/reset generation)."""

    toolchain = None

    # The stock TinyFPGA BX has no button; add one here so the auto-boot
    # sim config can exercise ButtonStaySource.
    resources = TinyFPGABXPlatform.resources + [
        Resource("button", 0, PinsN("A9", dir="i"), Attrs(IO_STANDARD="SB_LVCMOS")),
    ]
