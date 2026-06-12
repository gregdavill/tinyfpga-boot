"""Wake the SPI flash from deep power-down at boot.

After configuration the FPGA can leave the config flash in deep power-down. 
On iCE40 the config controller issues a ``0xB9`` (Deep Power-Down) as its last 
step of reading the bitstream. In that state the flash ignores every opcode except
``0xAB`` (Release Power-Down).

Clock a Mode-Bit-Reset (``0xFF``, exits any continuous-read mode) followed by
Release-Power-Down (``0xAB``), then waits out tRES1 so the flash is ready.
"""

from amaranth import Module, Signal
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring, stream, data

from blocks.qspi import Mode


MODE_BIT_RESET    = 0xFF   # exit continuous/performance read mode
RELEASE_POWERDOWN = 0xAB   # wake from deep power-down


class FlashWake(wiring.Component):
    needs_flash = True

    def __init__(self, *, settle_cycles=512):
        # tRES1 (CS# high before the flash accepts commands) is a few µs; 512
        # sync cycles is ~42 µs @ 12 MHz.
        self._settle = settle_cycles
        super().__init__({
            # FLASH QSPI interface
            "o": Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "i": In(stream.Signature(data.StructLayout({"data": 8}))),

            # Control
            "req":  In(1),
            "done": Out(1),
        })

    def elaborate(self, platform):
        m = Module()

        delay = Signal(range(self._settle + 1))

        def send(chip, mode, d=0):
            m.d.comb += [
                self.o.p.chip.eq(chip),
                self.o.p.mode.eq(mode),
                self.o.p.data.eq(d),
                self.o.valid.eq(1),
            ]

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.req):
                    m.next = "MBR"

            with m.State("MBR"):
                send(1, Mode.PutX1, MODE_BIT_RESET)
                with m.If(self.o.ready):
                    m.next = "MBR_RELEASE"

            with m.State("MBR_RELEASE"):
                send(0, Mode.Dummy)            # deassert CS#
                with m.If(self.o.ready):
                    m.next = "WAKE"

            with m.State("WAKE"):
                send(1, Mode.PutX1, RELEASE_POWERDOWN)
                with m.If(self.o.ready):
                    m.next = "WAKE_RELEASE"

            with m.State("WAKE_RELEASE"):
                send(0, Mode.Dummy)            # deassert CS#
                with m.If(self.o.ready):
                    m.d.sync += delay.eq(self._settle)
                    m.next = "SETTLE"

            with m.State("SETTLE"):
                with m.If(delay == 0):
                    m.next = "DONE"
                with m.Else():
                    m.d.sync += delay.eq(delay - 1)

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)

        return m


# Test cases
import unittest
from blocks.test_util import stream_get, simulate


class TestFlashWake(unittest.TestCase):
    def test_emits_mbr_then_release_then_settles(self):
        """The reader clocks 0xFF, releases CS, clocks 0xAB, releases CS, waits,
        then asserts `done`."""
        dut = FlashWake(settle_cycles=8)
        seen = []
        result = {}

        async def collect(ctx):
            # The QSPI command tokens appear on `o`; capture (chip, mode, data).
            for _ in range(4):
                tok = await stream_get(ctx, dut.o)
                seen.append((tok["chip"], tok["mode"], tok["data"]))

        async def drive(ctx):
            ctx.set(dut.req, 1)
            for _ in range(80):
                if ctx.get(dut.done):
                    break
                await ctx.tick()
            result["done"] = ctx.get(dut.done)

        simulate(dut, collect, drive)

        self.assertEqual(seen[0], (1, Mode.PutX1, MODE_BIT_RESET))
        self.assertEqual(seen[1][0], 0)                       # CS# released
        self.assertEqual(seen[2], (1, Mode.PutX1, RELEASE_POWERDOWN))
        self.assertEqual(seen[3][0], 0)                       # CS# released
        self.assertTrue(result["done"])


if __name__ == "__main__":
    unittest.main()
