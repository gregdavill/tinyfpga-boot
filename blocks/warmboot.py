from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class Warmboot(wiring.Component):
    """iCE40 warm-reboot trigger built on `SB_WARMBOOT`.

    Pulses `SB_WARMBOOT.BOOT` after a complete bitstream transfer has
    been received AND USB/flash activity has been quiet for at least
    `idle_threshold_cycles` consecutive cycles.

    Slot selection (`S1:S0`):
      * 0 (00) - cold-boot image (bootloader bitstream)
      * 1 (01) - additional image (user bitstream)
      * 2 (10), 3 (11) - additional user images (not used)

    The slot is fixed at elaboration time via the `slot` arg
    """

    def __init__(self, *, idle_threshold_cycles=600_000, slot=1):
        if not 0 <= slot <= 3:
            raise ValueError(f"slot must be 0..3, got {slot}")
        if idle_threshold_cycles < 1:
            raise ValueError("idle_threshold_cycles must be at least 1")
        self.idle_threshold_cycles = idle_threshold_cycles
        self.slot = slot
        super().__init__({
            # 1-cycle pulse or sustained level to arm. 
            # After arm, the FSM waits for `activity` to stay low for 
            # `idle_threshold_cycles` consecutive cycles.
            "arm":      In(1),
            "activity": In(1),
            # Output to the SB_WARMBOOT primitive. Useful for sim.
            "boot":     Out(1),
        })

    def elaborate(self, platform):
        m = Module()

        pending  = Signal()
        idle_cnt = Signal(range(self.idle_threshold_cycles + 1))
        boot     = Signal()

        with m.If(self.arm):
            m.d.sync += pending.eq(1)

        # Idle counter: reset on any activity, otherwise advance
        # while `pending` is asserted.
        with m.If(self.activity):
            m.d.sync += idle_cnt.eq(0)
        with m.Elif(pending & (idle_cnt != self.idle_threshold_cycles)):
            m.d.sync += idle_cnt.eq(idle_cnt + 1)

        # Fire BOOT once we've held `pending & idle` 
        m.d.comb += [
            boot.eq(pending & (idle_cnt == self.idle_threshold_cycles)),
            self.boot.eq(boot),
        ]

        m.submodules.sb_warmboot = Instance(
            "SB_WARMBOOT",
            i_BOOT=boot,
            i_S0=C(self.slot & 1, 1),
            i_S1=C((self.slot >> 1) & 1, 1),
        )

        return m
