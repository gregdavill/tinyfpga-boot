"""Generic priority arbiter over wiring.Signatures.

`Arbiter` muxes N producers onto one consumer with a pure combinational
priority scheme: the lowest-index active producer is granted the consumer, and
routing is generic.
"""

from amaranth import Module, Signal
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out, connect, flipped


class Arbiter(wiring.Component):
    def __init__(self, signature, n):
        self._n = n
        super().__init__({
            "inp":    In(signature).array(n),   # producer-facing ports (flipped)
            "out":    Out(signature),           # consumer-facing port
            "active": In(n),                    # per-producer activity
            "grant":  Out(range(n + 1)),        # granted producer, or n = idle
        })

    def elaborate(self, platform):
        m = Module()
        grant = self._grant(m)
        m.d.comb += self.grant.eq(grant)
        self._route(m, grant)
        return m

    def _grant(self, m):
        """Combinational priority: the lowest-index active producer, or n."""
        grant = Signal(range(self._n + 1))
        m.d.comb += grant.eq(self._n)
        for i in reversed(range(self._n)):
            with m.If(self.active[i]):
                m.d.comb += grant.eq(i)
        return grant

    def _route(self, m, grant):
        """Pass the granted input through to the output.
        `connect` handles signature members in both directions.
        idle/non-granted paths fall to their comb defaults (0)."""
        with m.Switch(grant):
            for i in range(self._n):
                with m.Case(i):
                    connect(m, flipped(self.inp[i]), flipped(self.out))


class HoldArbiter(Arbiter):
    """`Arbiter` whose grant latches and holds across brief idles.
    releasing only after `idle_hold` consecutive quiet cycles."""

    def __init__(self, signature, n, *, idle_hold=64):
        self._idle_hold = idle_hold
        super().__init__(signature, n)

    def _grant(self, m):
        n = self._n
        grant = Signal(range(n + 1), init=n)
        idle  = Signal(range(self._idle_hold + 1))

        with m.If(grant == n):
            # Idle: latch the lowest-index active producer (reversed so the
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
                            with m.If(idle == self._idle_hold):
                                m.d.sync += grant.eq(n)
        return grant
