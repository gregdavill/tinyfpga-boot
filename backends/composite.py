"""Composite backend to expose several USB personalities at once.

`CompositeBackend` is a `Backend`, so `Top` treats it like any single backend. 
It holds a collection of backends, fans the descriptor/handler/endpoint calls
out to them, and arbitrates the shared QSPI bus between them with a latched-priority
`QspiArbiter`.
"""

from amaranth import Module, Signal, Cat, Const, Mux
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out

from blocks.arbiter import HoldArbiter
from blocks.qspi import Mode

from . import (Backend, Reconfig, Status, UsbAlloc, BACKENDS,
               create_backing, bind_writers)


# The controller-facing QSPI bus a backing or raw backend drives.
_BUS = wiring.Signature({
    "qo": Out(stream.Signature(data.StructLayout({"chip": range(2), "mode": Mode, "data": 8}))),
    "qi": In(stream.Signature(data.StructLayout({"data": 8}))),
})


def _max_status(m, statuses):
    """Highest-severity status (ERROR>DONE>ACTIVE>IDLE by enum value)."""
    if len(statuses) == 1:
        return statuses[0]
    sev = Const(0, 2)
    for st in statuses:
        acc = Signal(2)
        m.d.comb += acc.eq(Mux(st.as_value() > sev, st, sev))
        sev = acc
    return sev


class CompositeBackend(Backend):
    # Multi-function device: the device descriptor uses the Interface
    # Association class so the host groups each function's interfaces correctly.
    device_class = (0xEF, 0x02, 0x01)
    personality = "Multi"

    def __init__(self, config, *, hs, kinds):
        super().__init__(config, hs=hs)
        self.usb_ids = (config.vid, config.pid)

        alloc = UsbAlloc()
        self.children = [BACKENDS[k](config, hs=hs, alloc=alloc) for k in kinds]

    def populate_configuration(self, c, *, bulk_mps):
        c.bMaxPower = 100
        for child in self.children:
            child.populate_configuration(c, bulk_mps=bulk_mps)

    def request_handlers(self):
        handlers = []
        for child in self.children:
            handlers += child.request_handlers()
        return handlers

    def msft_compat_ids(self):
        ids = []
        for child in self.children:
            ids += child.msft_compat_ids()
        return ids

    def endpoints(self):
        eps = []
        for child in self.children:
            eps += child.endpoints()
        return eps

    def build(self, m, *, usb):
        arms = []
        activities = []
        for idx, child in enumerate(self.children):
            # Build each child inside its own Module so their submodule
            # names can't collide.
            cm = Module()
            rc = child.build(cm, usb=usb)
            m.submodules[f"be{idx}_{child.personality}"] = cm
            arms.append(rc.arm)
            activities.append(rc.activity)

        # Flash-writer children share one backing; raw-QSPI children keep their
        # own qo/qi. Each becomes a bus "participant" (qo, qi, status).
        writers = [c for c in self.children if c.writes_flash]
        raw     = [c for c in self.children if not c.writes_flash]

        parts = []
        if writers:
            backing = create_backing(m, config=self.config, reset=usb.reset_detected)
            bind_writers(m, backing=backing, writers=writers)
            parts.append((backing.qo, backing.qi,
                          _max_status(m, [c.status for c in writers])))
        for c in raw:
            parts.append((c.qo, c.qi, c.status))

        if len(parts) == 1:
            # Only the backing on the bus — no arbiter needed.
            qo, qi, owner_st = parts[0]
            self.qo, self.qi = qo, qi
            bus_active = qo.valid | qi.ready
        else:
            # The bus needs the *held* grant: a flash writer's QSPI transaction
            # goes idle between command bytes, and the grant should survive gaps.
            m.submodules.bus_arb = arb = HoldArbiter(_BUS, len(parts))
            for i, (qo, qi, _st) in enumerate(parts):
                wiring.connect(m, qo, arb.inp[i].qo)
                wiring.connect(m, arb.inp[i].qi, qi)
                m.d.comb += arb.active[i].eq(qo.valid | qi.ready)
            self.qo, self.qi = arb.out.qo, arb.out.qi

            # Status follows the participant that owns the bus; `owner` latches
            # the last granted.
            owner = Signal(range(len(parts)))
            with m.Switch(arb.grant):
                for i in range(len(parts)):
                    with m.Case(i):
                        m.d.sync += owner.eq(i)
            owner_st = Signal(Status)
            with m.Switch(owner):
                for i, (_qo, _qi, st) in enumerate(parts):
                    with m.Case(i):
                        m.d.comb += owner_st.eq(st)
            bus_active = arb.grant != len(parts)

        # Avoid status dipping to IDLE between chunks.
        with m.If(bus_active & (owner_st == Status.IDLE)):
            m.d.comb += self.status.eq(Status.ACTIVE)
        with m.Else():
            m.d.comb += self.status.eq(owner_st)

        cfg = Signal()
        m.d.comb += cfg.eq(Cat(*(child.cfg_ctrl_o for child in self.children)).any())
        self.cfg_ctrl_o = cfg

        return Reconfig(arm=Cat(*arms).any(), activity=Cat(*activities).any())
