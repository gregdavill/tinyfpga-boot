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


_PWM_BITS = 8                       # PWM / brightness resolution
_PWM_MAX  = (1 << _PWM_BITS) - 1


def _level(m, status, cnt):
    """Per-state brightness envelope (0.._PWM_MAX) driven off `cnt`.

      IDLE   - triangle breathe        ACTIVE - blink (top-1 bit ~few Hz)
      DONE   - full on                 ERROR  - blink fast (top-2 bit)
    """
    w = len(cnt)
    ramp = cnt[w - 1 - _PWM_BITS:w - 1]     # high-res triangle ramp
    tri  = Mux(cnt[w - 1], ~ramp, ramp)     # up then down, 0.._PWM_MAX
    breathe = (tri * tri) >> _PWM_BITS       # gamma ~2 -> smooth perceived ramp

    blink      = Mux(cnt[w - 3], _PWM_MAX, 0)
    blink_fast = Mux(cnt[w - 4], _PWM_MAX, 0)

    level = Signal(_PWM_BITS)
    with m.Switch(status):
        with m.Case(Status.IDLE):
            m.d.comb += level.eq(breathe)
        with m.Case(Status.ACTIVE):
            m.d.comb += level.eq(blink)
        with m.Case(Status.DONE):
            m.d.comb += level.eq(_PWM_MAX)
        with m.Case(Status.ERROR):
            m.d.comb += level.eq(blink_fast)
    return level


def _pwm(cnt, level):
    """1-bit PWM: on for `level`/2**_PWM_BITS of each carrier period."""
    return cnt[0:_PWM_BITS] < level


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


class MultiplexRgbStatusLed(wiring.Component):
    """Animator for a multiplexed `n` x RGB bar: the RgbStatus colour-per-state,
    but with the brightness envelope phase-shifted per LED.

    Outputs are *logical* (active-high). The board maps them onto physical
    pins/polarity: `sel` is the one-hot (active-high) LED select.
    """

    def __init__(self, *, n, clk_freq=12_000_000):
        if n < 1:
            raise ValueError("n must be at least 1")
        self._n = n
        self._clk_freq = clk_freq
        super().__init__({
            "status": In(Status),
            "sel":    Out(n),   # one-hot LED select
            "rgb":    Out(3),   # (R, G, B) channel-on
        })

    def elaborate(self, platform):
        m = Module()
        cnt = _counter(m, self._clk_freq)
        w = len(cnt)

        # Colour per state (mirrors RgbStatusLed).
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

        # Scan the LEDs one-hot, dwelling per slot for a ~1 kHz full-bar refresh
        dwell_max = max(1, self._clk_freq // (self._n * 1000))
        slot  = Signal(range(self._n))
        dwell = Signal(range(dwell_max))
        with m.If(dwell == dwell_max - 1):
            m.d.sync += dwell.eq(0)
            with m.If(slot == self._n - 1):
                m.d.sync += slot.eq(0)
            with m.Else():
                m.d.sync += slot.eq(slot + 1)
        with m.Else():
            m.d.sync += dwell.eq(dwell + 1)

        # Phase-shift the envelope per LED
        phased = (cnt + (slot << (w - 4)))[:w]
        on = _pwm(cnt, _level(m, self.status, phased))

        colour = Cat(red, grn, blu)
        m.d.comb += [
            self.sel.eq(C(1) << slot),       # one-hot, logical
            self.rgb.eq(Mux(on, colour, 0)),
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
