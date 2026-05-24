"""System reconfiguration trigger.

A platform-agnostic wrapper for "reboot the FPGA into another bitstream
once a transfer completes". The functional design drives a uniform
interface; each platform supplies the concrete implementation (or a
no-op where the fabric has no such primitive).

    arm:      latch a pending reconfigure (e.g. uf2.done).
    activity: high while mid-transaction; reconfigure waits for quiet.
    boot:     pulses high when reconfiguration is triggered (status/probe).
"""

from amaranth import *
from amaranth.lib import wiring, io
from amaranth.lib.wiring import In, Out

from .warmboot import Warmboot


class SystemReconfigure(wiring.Component):
    """Interface No-op class"""

    arm:      In(1)
    activity: In(1)
    boot:     Out(1)

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
    """Reconfigure via a board-level open-drain pin connected to PROGRAMN line."""

    def __init__(self, *, idle_cycles=600_000):
        if idle_cycles < 1:
            raise ValueError("idle_cycles must be at least 1")
        self._idle_cycles = idle_cycles
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

        fire = pending & (idle_cnt == self._idle_cycles)
        m.d.comb += self.boot.eq(fire)

        # Open-drain: hold the pin low (o=0, oe asserted) to reconfigure,
        # release to hi-Z otherwise.
        port = platform.request("reconfigure", dir="-")
        m.submodules.buffer = buffer = io.Buffer("io", port)
        m.d.comb += [
            buffer.o.eq(0),
            buffer.oe.eq(fire),
        ]

        return m
