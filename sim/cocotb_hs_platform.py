"""`CocotbHSPlatform` - `ECPBreakerR3_0Platform` with the toolchain skipped.

The high-speed (ULPI) sim target. It keeps the real ecpbreaker resources and
the `ECP5Mixin` clock/reset generation, so the elaborated design is exactly
what the board build produces.
"""

from config.ecpbreaker import ECPBreakerR3_0Platform
from sim.cocotb_platform import _RawPortCaptureMixin


class CocotbHSPlatform(_RawPortCaptureMixin, ECPBreakerR3_0Platform):
    """Sim-friendly view of the ECPBreaker r3.0 (ECP5) high-speed platform."""

    toolchain = None
