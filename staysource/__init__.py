"""Pluggable "stay in the bootloader" sources.

On power-on the bootloader normally reboots straight into the slot-1
application (see `Top`'s boot FSM). A `StaySource` is a small component that
can veto that and keep the bootloader resident so it enumerates over USB.
`Top` collects the configured sources, ORs their `stay` outputs, and only
auto-boots the app when *none* of them asserts.
"""

from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out

from blocks.qspi import Mode


# QSPI command / response stream layouts (shared with the controller).
QSPI_CMD = data.StructLayout({"chip": range(2), "mode": Mode, "data": 8})
QSPI_RESP = data.StructLayout({"data": 8})


class StaySource(wiring.Component):
    """Base class. Subclasses set `needs_flash` and implement `elaborate`.

    Ports:
      stay : Out(1)  -- assert to keep the bootloader (suppress auto-boot)
      req  : In(1)   -- (flash sources) Top asserts while this source owns QSPI
      done : Out(1)  -- (flash sources) read complete; `stay` valid
      o/i  : (flash sources only) QSPI command/response streams
    """

    #: Whether this source reads flash during BOOT-READ.
    needs_flash = False

    def __init__(self):
        members = {
            "stay": Out(1),
            "req":  In(1),
            "done": Out(1),
        }
        if self.needs_flash:
            members["o"] = Out(stream.Signature(QSPI_CMD))
            members["i"] = In(stream.Signature(QSPI_RESP))
        super().__init__(members)


from .always import AlwaysStaySource             # noqa: E402,F401
from .button import ButtonStaySource            # noqa: E402,F401
from .no_valid_app import NoValidAppStaySource   # noqa: E402,F401
from .write_enable import WriteEnableStaySource  # noqa: E402,F401
