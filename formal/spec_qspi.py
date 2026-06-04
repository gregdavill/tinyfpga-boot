"""Formal specs for the QSPI `Enframer` and `Deframer`.

Interface-level protocol properties:
  * Enframer never accepts an octet unless the frame sink also accepted it this
    cycle (no octet silently dropped), parks the clock high when no chip is
    selected (Motorola Mode 3 idle).
  * Deframer only ever produces an output octet in response to a valid input
    frame, and only for the data-receiving (Get / Swap) modes.
"""

from amaranth import *
from amaranth.hdl import Assert, Assume, Cover
from amaranth.hdl._ast import AnySeq
from amaranth.lib import io

from blocks.ports import PortGroup
from blocks.qspi import Enframer, Deframer, Mode


def _ports():
    # Mirrors Controller's inner PortGroup: 1 chip-select, single-bit clk, and
    # four bidirectional data lines.
    return PortGroup(
        cs=io.SimulationPort("o", 1),
        sck=io.SimulationPort("o", 1),
        io0=io.SimulationPort("io", 1),
        io1=io.SimulationPort("io", 1),
        io2=io.SimulationPort("io", 1),
        io3=io.SimulationPort("io", 1),
    )


class EnframerSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = Enframer(ports=_ports())

        m.d.comb += [
            dut.octets.valid.eq(AnySeq(1)),
            dut.octets.p.chip.eq(AnySeq(len(dut.octets.p.chip))),
            dut.octets.p.mode.eq(AnySeq(3)),
            dut.octets.p.data.eq(AnySeq(8)),
            dut.frames.ready.eq(AnySeq(1)),
            dut.divisor.eq(AnySeq(16)),
        ]
        # Keep the clock divisor small so a full octet covers at shallow depth;
        # the handshake/idle properties hold for any divisor.
        m.d.comb += Assume(dut.divisor <= 1)

        # Never consume an octet unless the frame sink accepted it this cycle.
        m.d.comb += Assert(
            ~dut.octets.ready | (dut.octets.valid & dut.frames.ready))

        # Deselected (chip 0) parks SCK high in both half-phases.
        with m.If(dut.octets.p.chip == 0):
            m.d.comb += Assert(dut.frames.p.port.sck.o.as_value() == 0b11)

        # A full octet can actually be clocked out.
        m.d.comb += Cover(dut.octets.ready)

        return m


class DeframerSpec(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.dut = dut = Deframer(ports=_ports())

        m.d.comb += [
            dut.frames.valid.eq(AnySeq(1)),
            dut.frames.p.meta.mode.eq(AnySeq(3)),
            dut.frames.p.meta.half.eq(AnySeq(1)),
            dut.frames.p.port.io0.i.eq(AnySeq(2)),
            dut.frames.p.port.io1.i.eq(AnySeq(2)),
            dut.frames.p.port.io2.i.eq(AnySeq(2)),
            dut.frames.p.port.io3.i.eq(AnySeq(2)),
            dut.octets.ready.eq(AnySeq(1)),
        ]

        mode = dut.frames.p.meta.mode
        is_get = ((mode == Mode.GetX1) | (mode == Mode.GetX2)
                  | (mode == Mode.GetX4) | (mode == Mode.Swap))

        # An output octet only appears in response to a valid input frame...
        m.d.comb += Assert(~dut.octets.valid | dut.frames.valid)
        # ...and only for the data-receiving modes (never Dummy / Put).
        m.d.comb += Assert(~dut.octets.valid | is_get)

        m.d.comb += Cover(dut.octets.valid & dut.octets.ready)

        return m


if __name__ == "__main__":
    from formal._runner import verify
    verify(EnframerSpec(), name="qspi_enframer", depth=20)
    verify(DeframerSpec(), name="qspi_deframer", depth=12)
