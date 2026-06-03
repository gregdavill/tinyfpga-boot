"""Status indicator LED drivers.

Take `Status` (IDLE / ACTIVE / DONE / ERROR) and show on LEDs.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from backends import Status


def _counter(m, clk_freq):
    """Free-running counter sized so its top bit cycles ~once per second."""
    width = max(9, int(clk_freq).bit_length())
    cnt = Signal(width)
    m.d.sync += cnt.eq(cnt + 1)
    return cnt


def _level(m, status, cnt):
    """Per-state 4-bit brightness envelope driven off `cnt`.

      IDLE   - triangle breathe        ACTIVE - blink (top-1 bit ~few Hz)
      DONE   - full on                 ERROR  - blink fast (top-2 bit)
    """
    w = len(cnt)
    ramp = cnt[w - 5:w - 1]                 # slow 4-bit ramp
    breathe = Mux(cnt[w - 1], ~ramp, ramp)  # up then down
    blink = Mux(cnt[w - 3], 0xF, 0)
    blink_fast = Mux(cnt[w - 4], 0xF, 0)

    level = Signal(4)
    with m.Switch(status):
        with m.Case(Status.IDLE):
            m.d.comb += level.eq(breathe)
        with m.Case(Status.ACTIVE):
            m.d.comb += level.eq(blink)
        with m.Case(Status.DONE):
            m.d.comb += level.eq(0xF)
        with m.Case(Status.ERROR):
            m.d.comb += level.eq(blink_fast)
    return level


def _pwm(cnt, level):
    """1-bit PWM: on for `level`/16 of each carrier period."""
    return cnt[0:4] < level


class MonoStatusLed(wiring.Component):
    """Single active-high LED."""

    status: In(Status)
    led:    Out(1)

    def __init__(self, *, clk_freq=12_000_000):
        self._clk_freq = clk_freq
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        cnt = _counter(m, self._clk_freq)
        level = _level(m, self.status, cnt)
        m.d.comb += self.led.eq(_pwm(cnt, level))
        return m


class RgbStatusLed(wiring.Component):
    """Common-cathode RGB LED: a colour per state, animated by `_level`.

      IDLE green (breathe) / ACTIVE blue (blink) /
      DONE green (solid)   / ERROR red (blink fast)
    """

    status: In(Status)
    r:      Out(1)
    g:      Out(1)
    b:      Out(1)

    def __init__(self, *, clk_freq=12_000_000):
        self._clk_freq = clk_freq
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        cnt = _counter(m, self._clk_freq)
        on = _pwm(cnt, _level(m, self.status, cnt))

        red = Signal()
        grn = Signal()
        blu = Signal()
        with m.Switch(self.status):
            with m.Case(Status.IDLE):
                m.d.comb += grn.eq(1)
            with m.Case(Status.ACTIVE):
                m.d.comb += blu.eq(1)
            with m.Case(Status.DONE):
                m.d.comb += grn.eq(1)
            with m.Case(Status.ERROR):
                m.d.comb += red.eq(1)

        m.d.comb += [
            self.r.eq(red & on),
            self.g.eq(grn & on),
            self.b.eq(blu & on),
        ]
        return m


class SequenceStatusLed(wiring.Component):
    """Bar of `n` active-high LEDs.

      IDLE   - all breathe together     ACTIVE - a single lit LED chases
      DONE   - all solid on             ERROR  - all blink fast together
    """

    def __init__(self, *, n, clk_freq=12_000_000):
        if n < 1:
            raise ValueError("n must be at least 1")
        self._n = n
        self._clk_freq = clk_freq
        super().__init__({"status": In(Status), "leds": Out(n)})

    def elaborate(self, platform):
        m = Module()
        cnt = _counter(m, self._clk_freq)
        on = _pwm(cnt, _level(m, self.status, cnt))

        w = len(cnt)
        pos_bits = max(1, (self._n - 1).bit_length())
        pos = cnt[w - 3 - pos_bits:w - 3]  # step the chase a few Hz

        with m.Switch(self.status):
            with m.Case(Status.ACTIVE):
                m.d.comb += self.leds.eq(C(1) << pos)
            with m.Case(Status.DONE):
                m.d.comb += self.leds.eq(C(-1, self._n))
            with m.Default():  # IDLE / ERROR: all LEDs share the envelope
                m.d.comb += self.leds.eq(on.replicate(self._n))
        return m
