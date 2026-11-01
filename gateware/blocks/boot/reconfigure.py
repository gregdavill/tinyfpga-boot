"""System reconfiguration trigger.

A platform-agnostic wrapper for "reboot the FPGA into another bitstream
once a transfer completes". The functional design drives a uniform
interface; each platform supplies the concrete implementation (or a
no-op where the fabric has no such primitive).

    arm:      latch a pending reconfigure (e.g. uf2.done).
    activity: high while mid-transaction; reconfigure waits for quiet.
    boot:     pulses high when reconfiguration is triggered (status/probe).
    phy_reset: high while a USB PHY is held in reset ahead of `boot`.
"""

from amaranth import *
from amaranth.lib import wiring, io
from amaranth.lib.wiring import In, Out

from .warmboot import Warmboot


class SystemReconfigure(wiring.Component):
    """Interface No-op class"""

    arm:       In(1)
    activity:  In(1)
    boot:      Out(1)
    phy_reset: Out(1)

    def elaborate(self, platform):
        return Module()


class WarmbootReconfigure(SystemReconfigure):
    """iCE40: reboot into `slot` via SB_WARMBOOT once `arm` has latched and
    `activity` has been quiet for `idle_cycles`."""

    def __init__(self, *, slot=1, idle_cycles=600_000):
        self._slot = slot
        self._idle_cycles = idle_cycles
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.warmboot = wb = Warmboot(
            idle_threshold_cycles=self._idle_cycles, slot=self._slot)
        m.d.comb += [
            wb.arm.eq(self.arm),
            wb.activity.eq(self.activity),
            self.boot.eq(wb.boot),
        ]
        return m


class OpenDrainReconfigure(SystemReconfigure):
    """Reconfigure via a board-level open-drain pin connected to PROGRAMN line.

    Once the idle window elapses, `phy_reset` holds for `phy_reset_cycles`
    before PROGRAMN is pulled, and stays high until the FPGA goes away. The
    decision is latched at that point: `activity` no longer defers it.
    """

    def __init__(self, *, idle_cycles=600_000, phy_reset_cycles=0):
        if idle_cycles < 1:
            raise ValueError("idle_cycles must be at least 1")
        if phy_reset_cycles < 0:
            raise ValueError("phy_reset_cycles must not be negative")
        self._idle_cycles = idle_cycles
        self._phy_reset_cycles = phy_reset_cycles
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        pending  = Signal()
        idle_cnt = Signal(range(self._idle_cycles + 1))

        with m.If(self.arm):
            m.d.sync += pending.eq(1)

        # Idle counter: reset on activity, otherwise advance while pending.
        with m.If(self.activity):
            m.d.sync += idle_cnt.eq(0)
        with m.Elif(pending & (idle_cnt != self._idle_cycles)):
            m.d.sync += idle_cnt.eq(idle_cnt + 1)

        holding  = Signal()
        hold_cnt = Signal(range(self._phy_reset_cycles + 1))

        with m.If(pending & (idle_cnt == self._idle_cycles)):
            m.d.sync += holding.eq(1)
        with m.If(holding & (hold_cnt != self._phy_reset_cycles)):
            m.d.sync += hold_cnt.eq(hold_cnt + 1)

        fire = holding & (hold_cnt == self._phy_reset_cycles)
        m.d.comb += [
            self.boot.eq(fire),
            self.phy_reset.eq(holding),
        ]

        port = platform.request("reconfigure", dir="o")
        m.d.comb += [
            port.o.eq(fire),
        ]

        return m
