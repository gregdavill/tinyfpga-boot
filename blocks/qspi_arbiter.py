"""Latching priority QSPI bus arbiter.

Multiplexes N backends' `(qo, qi)` QSPI stream pairs onto the single shared
controller.

Latches the grant to the lowest-index backend that raises `active`, holds it
while that backend stays active, and releases only after a quiet window.
"""

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from .qspi import Mode


# Quiet sync cycles a granted backend must be idle before the bus is released.
_IDLE_HOLD = 64

_QO_LAYOUT = data.StructLayout({"chip": range(2), "mode": Mode, "data": 8})
_QI_LAYOUT = data.StructLayout({"data": 8})


class QspiArbiter(wiring.Component):
    def __init__(self, n):
        self.n = n
        super().__init__({
            # Per-backend QSPI streams (matching the controller-facing pair each
            # backend exposes).
            "child_qo": In(stream.Signature(_QO_LAYOUT)).array(n),
            "child_qi": Out(stream.Signature(_QI_LAYOUT)).array(n),
            # Per-backend activity; drives grant selection.
            "active":   In(n),
            # Shared controller-facing streams.
            "qo":       Out(stream.Signature(_QO_LAYOUT)),
            "qi":       In(stream.Signature(_QI_LAYOUT)),
            # Selected backend index, or `n` when the bus is idle.
            "grant":    Out(range(n + 1)),
        })

    def elaborate(self, platform):
        m = Module()
        n = self.n

        grant = Signal(range(n + 1), init=n)
        idle  = Signal(range(_IDLE_HOLD + 1))
        m.d.comb += self.grant.eq(grant)

        with m.If(grant == n):
            # Idle: latch the lowest-index active backend (reversed loop so the
            # lowest index wins the priority).
            for i in reversed(range(n)):
                with m.If(self.active[i]):
                    m.d.sync += grant.eq(i)
            m.d.sync += idle.eq(0)
        with m.Else():
            with m.Switch(grant):
                for i in range(n):
                    with m.Case(i):
                        with m.If(self.active[i]):
                            m.d.sync += idle.eq(0)
                        with m.Else():
                            m.d.sync += idle.eq(idle + 1)
                            with m.If(idle == _IDLE_HOLD):
                                m.d.sync += grant.eq(n)

        # qo: granted backend -> controller; ready fans back to the granted one.
        m.d.comb += self.qo.valid.eq(0)
        with m.Switch(grant):
            for i in range(n):
                with m.Case(i):
                    m.d.comb += [
                        self.qo.p.eq(self.child_qo[i].p),
                        self.qo.valid.eq(self.child_qo[i].valid),
                    ]
        for i in range(n):
            m.d.comb += self.child_qo[i].ready.eq((grant == i) & self.qo.ready)

        # qi: controller response -> granted backend only.
        m.d.comb += self.qi.ready.eq(0)
        with m.Switch(grant):
            for i in range(n):
                with m.Case(i):
                    m.d.comb += self.qi.ready.eq(self.child_qi[i].ready)
        for i in range(n):
            m.d.comb += [
                self.child_qi[i].p.eq(self.qi.p),
                self.child_qi[i].valid.eq((grant == i) & self.qi.valid),
            ]

        return m
