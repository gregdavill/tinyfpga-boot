"""Composite backend to expose several USB personalities at once.

`CompositeBackend` is a `Backend`, so `Top` treats it like any single backend. 
It holds a collection of backends, fans the descriptor/handler/endpoint calls
out to them, and arbitrates the shared QSPI bus between them with a latched-priority
`QspiArbiter`.
"""

from amaranth import Module, Signal, Cat
from amaranth.lib import wiring

from blocks.qspi_arbiter import QspiArbiter

from . import Backend, Reconfig, Status, UsbAlloc, BACKENDS


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

    def endpoints(self):
        eps = []
        for child in self.children:
            eps += child.endpoints()
        return eps

    def build(self, m, *, usb):
        n = len(self.children)
        m.submodules.qspi_arb = arb = QspiArbiter(n)

        arms = []
        activities = []
        for idx, child in enumerate(self.children):
            # Build each child inside its own Module so their submodule
            # names can't collide.
            cm = Module()
            rc = child.build(cm, usb=usb)
            m.submodules[f"be{idx}_{child.personality}"] = cm

            wiring.connect(m, child.qo, arb.child_qo[idx])
            wiring.connect(m, arb.child_qi[idx], child.qi)
            
            # Arbitrate on QSPI bus use
            m.d.comb += arb.active[idx].eq(child.qo.valid | child.qi.ready)

            arms.append(rc.arm)
            activities.append(rc.activity)

        self.qo, self.qi = arb.qo, arb.qi

        # Status follows the backend that owns the bus. `owner` latches the last granted
        owner = Signal(range(n))
        with m.Switch(arb.grant):
            for idx in range(n):
                with m.Case(idx):
                    m.d.sync += owner.eq(idx)

        owner_st = Signal(Status)
        with m.Switch(owner):
            for idx, child in enumerate(self.children):
                with m.Case(idx):
                    m.d.comb += owner_st.eq(child.status)

        # Avoid status dipping to IDLE between chunks
        with m.If((arb.grant != n) & (owner_st == Status.IDLE)):
            m.d.comb += self.status.eq(Status.ACTIVE)
        with m.Else():
            m.d.comb += self.status.eq(owner_st)

        cfg = Signal()
        m.d.comb += cfg.eq(Cat(*(child.cfg_ctrl_o for child in self.children)).any())
        self.cfg_ctrl_o = cfg

        return Reconfig(arm=Cat(*arms).any(), activity=Cat(*activities).any())
