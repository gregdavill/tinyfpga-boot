"""Formal spec for `Warmboot`.

Properties of interest, expressed purely over the interface (arm/activity/boot):
  * boot only ever fires after `arm` has been seen at least once, and
  * boot only fires after `activity` has stayed low for the full idle threshold.

Threshold shrunk to keep depth small
"""

from amaranth import *
from amaranth.hdl import Assert, Cover
from amaranth.hdl._ast import AnySeq

from blocks.warmboot import Warmboot
from pathlib import Path


THRESH = 3


class WarmbootSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = Warmboot(idle_threshold_cycles=THRESH, slot=1)

        m.d.comb += [
            dut.arm.eq(AnySeq(1)),
            dut.activity.eq(AnySeq(1)),
        ]

        # Interface-only shadow state: was the trigger ever armed, and how many
        # consecutive cycles has activity stayed low (saturating at THRESH).
        armed_ever = Signal()
        with m.If(dut.arm):
            m.d.sync += armed_ever.eq(1)

        idle_run = Signal(range(THRESH + 1))
        with m.If(dut.activity):
            m.d.sync += idle_run.eq(0)
        with m.Elif(idle_run != THRESH):
            m.d.sync += idle_run.eq(idle_run + 1)

        m.d.comb += Assert(~dut.boot | armed_ever)
        m.d.comb += Assert(~dut.boot | (idle_run == THRESH))

        # The trigger is actually reachable (not vacuously never-fires).
        m.d.comb += Cover(dut.boot)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    stub = Path(__file__).resolve().parent / "cells_stub.v"
    verify(WarmbootSpec(), name="warmboot", depth=THRESH + 6, lib_files=[stub])
