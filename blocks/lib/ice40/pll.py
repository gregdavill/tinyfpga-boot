from amaranth import *
from amaranth.build import Platform

__all__ = ["ICE40PLL"]


class ICE40PLL(Elaboratable):
    """iCE40 PLL.

    Instantiates the SB_PLL40_CORE primitive in SIMPLE-feedback mode and
    provides a single global clock output. The divisors (DIVR / DIVF /
    DIVQ) and the loop FILTER_RANGE are computed from the requested input
    and output frequencies.
    """
    num_clkouts_max = 1

    divr_range = (0, 15 + 1)
    divf_range = (0, 63 + 1)
    divq_range = (1, 6 + 1)
    clki_freq_range = (10e6, 133e6)
    clko_freq_range = (16e6, 275e6)
    pfd_freq_range = (10e6, 133e6)
    vco_freq_range = (533e6, 1066e6)

    def __init__(self):
        self.reset = Signal()
        self.locked = Signal()
        self.clkin_freq = None
        self.clkin = None
        self.clkout = None          # (clock_domain, freq, margin)
        self.config = {}
        self.params = {}

    def register_clkin(self, clkin, freq):
        (clki_freq_min, clki_freq_max) = self.clki_freq_range
        if freq < clki_freq_min:
            raise ValueError("Input clock frequency ({!r}) is lower than the minimum allowed input clock frequency ({!r})"
                             .format(freq, clki_freq_min))
        if freq > clki_freq_max:
            raise ValueError("Input clock frequency ({!r}) is higher than the maximum allowed input clock frequency ({!r})"
                             .format(freq, clki_freq_max))
        self.clkin = clkin
        self.clkin_freq = freq

    def create_clkout(self, cd, freq, margin=1e-2):
        (clko_freq_min, clko_freq_max) = self.clko_freq_range
        if freq < clko_freq_min:
            raise ValueError("Requested output clock frequency ({!r}) is lower than the minimum allowed output clock frequency ({!r})"
                             .format(freq, clko_freq_min))
        if freq > clko_freq_max:
            raise ValueError("Requested output clock frequency ({!r}) is higher than the maximum allowed output clock frequency ({!r})"
                             .format(freq, clko_freq_max))
        if self.clkout is not None:
            raise ValueError("SB_PLL40_CORE provides a single output clock")
        self.clkout = (cd, freq, margin)

    @staticmethod
    def _filter_range(f_pfd):
        # Loop-filter range vs phase-frequency-detector frequency (MHz),
        # following icepll.
        f_pfd_mhz = f_pfd / 1e6
        for threshold, value in ((17, 1), (26, 2), (44, 3), (66, 4), (101, 5)):
            if f_pfd_mhz < threshold:
                return value
        return 6

    def compute_config(self):
        (_cd, freq, margin) = self.clkout
        (pfd_min, pfd_max) = self.pfd_freq_range
        (vco_min, vco_max) = self.vco_freq_range
        for divr in range(*self.divr_range):
            f_pfd = self.clkin_freq / (divr + 1)
            if not (pfd_min <= f_pfd <= pfd_max):
                continue
            for divf in range(*self.divf_range):
                f_vco = f_pfd * (divf + 1)
                if not (vco_min <= f_vco <= vco_max):
                    continue
                for divq in range(*self.divq_range):
                    f_out = f_vco / (2 ** divq)
                    if abs(f_out - freq) <= freq * margin:
                        return {
                            "divr": divr, "divf": divf, "divq": divq,
                            "filter_range": self._filter_range(f_pfd),
                            "vco": f_vco, "freq": f_out,
                        }
        raise ValueError("No PLL config found")

    def elaborate(self, platform: Platform) -> Module:
        m = Module()

        config = self.compute_config()
        (cd, _freq, _margin) = self.clkout

        m.submodules.pll = Instance(
            "SB_PLL40_CORE",
            p_FEEDBACK_PATH="SIMPLE",
            p_DIVR=config["divr"],
            p_DIVF=config["divf"],
            p_DIVQ=config["divq"],
            p_FILTER_RANGE=config["filter_range"],
            i_REFERENCECLK=self.clkin,
            i_RESETB=~self.reset,       # active-low reset
            i_BYPASS=Const(0),
            o_PLLOUTGLOBAL=ClockSignal(cd.name),
            o_LOCK=self.locked,
        )

        return m
