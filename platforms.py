"""Project platforms.

Each board platform owns its clock-and-reset generation (PLL + clock
domains + power-on reset) behind a uniform ``create_clocks(m)`` method,
so `top.py` stays purely functional and never touches an `SB_PLL40_CORE`
or `EHXPLLL` directly. Two families:

* iCE40 (`_ICE40Mixin`): TinyFPGA BX — full-speed (48 MHz usb_io + 12 MHz
  sync). (A ULPI high-speed variant was tried but the LP8K can't close
  60 MHz timing — see git history — so only ECP5 carries the HS target.)
* ECP5 (`OrangeCrabR0_2ULPIPlatform`): OrangeCrab r0.2 — 60 MHz sync.

The ULPI bus and (on ECP5) the clock-less SPI flash are placeholder pin
mappings for timing exploration, not real PHY wiring.
"""

from amaranth import *

from blocks.lib.ecp5.pll import ECP5PLL
from blocks.reconfigure import WarmbootReconfigure, OpenDrainReconfigure


# ---------------------------------------------------------------------------
# Project-platform properties read by Top (so Top stays platform-agnostic)
# ---------------------------------------------------------------------------

class _ProjectPlatform:
    """Board/family characteristics the functional Top reads off the
    platform instead of carrying as config:

      fpga_family - "ice40" | "ecp5"
      usb_phy     - "gateware_fs" (FS, D+/D- gateware PHY) | "ulpi_hs"
      flash_clk   - "pad" (DDR I/O pad) | "usrmclk" (ECP5 config MCLK)
      is_hs       - derived: True for the high-speed (ULPI) configuration
    """
    fpga_family = "ice40"
    usb_phy     = "gateware_fs"
    flash_clk   = "pad"
    default_usb_connection = "usb"


    @property
    def is_hs(self):
        return self.usb_phy == "ulpi_hs"


# ---------------------------------------------------------------------------
# iCE40 platform primitives (clock/reset + warmboot)
# ---------------------------------------------------------------------------

class ICE40Mixin(_ProjectPlatform):
    """iCE40-specific primitive helpers: PLL/reset (SB_PLL40_CORE via
    ICE40PLL) and the SB_WARMBOOT reload trigger."""

    def create_reconfigure(self, m, *, arm, activity, reset, slot=1,
                           idle_cycles=600_000):
        """SB_WARMBOOT-backed reconfigure trigger. Reboots into `slot` once
        `arm` has latched and `activity` has been quiet for `idle_cycles`;
        `reset` (a USB bus reset) cancels a pending reload."""
        m.submodules.reconfigure = rc = ResetInserter(reset)(
            WarmbootReconfigure(slot=slot, idle_cycles=idle_cycles))
        m.d.comb += [
            rc.arm.eq(arm),
            rc.activity.eq(activity),
        ]
        return rc

    def _por(self, m, *, clk_domain, freq, locked, reset_domains):
        """Power-on reset: hold `reset_domains` until the PLL locks and a
        >3 µs iCE40 BRAM-errata window (clocked by `clk_domain`) elapses."""
        cd_por = ClockDomain("por", reset_less=True, local=True)
        m.domains += cd_por
        m.d.comb += cd_por.clk.eq(ClockSignal(clk_domain))

        delay = int(5 * 3e-6 * freq)  # ~15 µs
        por_timer = Signal(range(delay + 1))
        por_ready = Signal()
        with m.If(por_timer == delay):
            m.d.por += por_ready.eq(1)
        with m.Else():
            m.d.por += por_timer.eq(por_timer + 1)

        for cd in reset_domains:
            m.d.comb += cd.rst.eq(~por_ready | ~locked)

# ---------------------------------------------------------------------------
# ECP5 platform primitives (clock/reset)
# ---------------------------------------------------------------------------

class ECP5Mixin(_ProjectPlatform):
    """ECP5-specific primitive helpers"""

    fpga_family = "ecp5"
    # usb_phy     = "ulpi_hs"
    flash_clk   = "usrmclk"

    def create_clocks(self, m):
        cd_sync = ClockDomain("sync")
        m.domains += cd_sync

        # Reference clock + frequency both come from the board's default_clk
        # resource (its declared Clock()), so there's no separate constant.
        clk_in = self.request(self.default_clk, dir="i").i
        clk_freq = self.default_clk_frequency

        if self.is_hs:
            m.submodules.pll = pll = ECP5PLL()
            pll.register_clkin(clk_in, clk_freq)
            pll.create_clkout(cd_sync, 60e6)
            self.add_clock_constraint(cd_sync.clk, 60e6)

            # Reset until the PLL locks.
            m.d.comb += cd_sync.rst.eq(~pll.locked)
        else:
            cd_usb_io = ClockDomain("usb_io")
            m.domains += cd_usb_io

            m.submodules.pll = pll = ECP5PLL()
            pll.register_clkin(clk_in, clk_freq)
            pll.create_clkout(cd_usb_io, 48e6)
            self.add_clock_constraint(cd_usb_io.clk, 48e6)

            # 12 MHz sync = usb_io / 4.
            div4 = Signal(range(4))
            m.d.usb_io += div4.eq(div4 + 1)
            m.d.comb += cd_sync.clk.eq(div4[-1])

            # Reset until the PLL locks.
            m.d.comb += cd_usb_io.rst.eq(~pll.locked)
            m.d.comb += cd_sync.rst.eq(~pll.locked)

    def create_reconfigure(self, m, *, arm, activity, reset, slot=1,
                           idle_cycles=600_000):
        # ECP5 has no primitive for reconfiguration; The bitstream can
        # specify a "BOOTADDR", this sets the starting address sent to 
        # the FLASH when the FPGA reconfigures.
        # 
        # Pulling PROGRAMN pin low will reload from flash,
        # A i/o pin is connected to PROGRAMN at board level
        m.submodules.reconfigure = rc = ResetInserter(reset)(
            OpenDrainReconfigure(idle_cycles=idle_cycles))
        m.d.comb += [
            rc.arm.eq(arm),
            rc.activity.eq(activity),
        ]
        return rc
