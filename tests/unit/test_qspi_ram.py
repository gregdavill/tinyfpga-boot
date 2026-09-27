import unittest
from tests.unit.util import simulate
from gateware.blocks.qspi.controller import Mode
from gateware.blocks.qspi.ram import ENTER_QPI, EXIT_QPI, QspiRam, WRITE


def _collect(dut, *, bytes_in, tcem_at=None, max_cycles=400):
    """Drive `bytes_in` (list of (addr, data)) into the writer with `qo` always
    accepted, and record the (chip, mode, data) octets emitted.

    `tcem_at`, if set, holds `tcem_expired` high while exactly that many input
    bytes have been consumed.

    Returns (octets, reached_boot_ready)."""
    octets = []
    idx = 0
    done_set = False
    reached = False

    async def tb(ctx):
        nonlocal idx, done_set, reached
        ctx.set(dut.qo.ready, 1)
        for _ in range(max_cycles):
            # tCEM watchdog (set first: WRITE_NEXT's i.ready depends on it).
            ctx.set(dut.tcem_expired, 1 if (tcem_at is not None and idx == tcem_at
                                            and not done_set) else 0)

            # Present the next input byte whenever the writer can take one.
            if ctx.get(dut.i.ready) and idx < len(bytes_in):
                ctx.set(dut.i.valid, 1)
                ctx.set(dut.i.p.addr, bytes_in[idx][0])
                ctx.set(dut.i.p.data, bytes_in[idx][1])
            else:
                ctx.set(dut.i.valid, 0)

            if ctx.get(dut.qo.valid):
                octets.append((ctx.get(dut.qo.p.chip),
                               ctx.get(dut.qo.p.mode),   # a Mode enum member
                               ctx.get(dut.qo.p.data)))

            consumed = ctx.get(dut.i.valid) and ctx.get(dut.i.ready)
            await ctx.tick()
            if consumed:
                idx += 1
            if idx >= len(bytes_in) and not done_set:
                ctx.set(dut.done, 1)
                done_set = True
            if ctx.get(dut.boot_ready):
                reached = True
                break

    simulate(dut, tb)
    return octets, reached

# (chip, mode, data) shorthand for a CS release
_DUMMY = (0, Mode.Dummy, 0)


class TestQspiRam(unittest.TestCase):
    def test_contiguous_write(self):
        """Swap -> exit QPI -> enter QPI -> single 0x02 burst with 3 addr bytes
        + data, then boot-arm (FLASH pulse) and boot_ready."""
        dut = QspiRam()
        octets, reached = _collect(
            dut,
            bytes_in=[(0x000010, 0xAA), (0x000011, 0xBB), (0x000012, 0xCC)])

        self.assertTrue(reached, "writer never reached boot_ready")

        # Chip-selected command octets (the swap pulse is a chip=1 Dummy).
        cmds = [(c, m, d) for (c, m, d) in octets if c == 1 and m != Mode.Dummy]
        # Defensive exit-QPI (x4), then enter-QPI (single line).
        self.assertEqual(cmds[0], (1, Mode.PutX4, EXIT_QPI))
        self.assertEqual(cmds[1], (1, Mode.PutX1, ENTER_QPI))
        # Then the write opcode + 3 address bytes (all x4).
        self.assertEqual(cmds[2], (1, Mode.PutX4, WRITE))
        self.assertEqual(cmds[3], (1, Mode.PutX4, 0x00))  # addr[23:16]
        self.assertEqual(cmds[4], (1, Mode.PutX4, 0x00))  # addr[15:8]
        self.assertEqual(cmds[5], (1, Mode.PutX4, 0x10))  # addr[7:0]
        # Then the three contiguous data bytes, one 0x02 burst (no reopen).
        self.assertEqual([d for (c, m, d) in cmds[6:9]], [0xAA, 0xBB, 0xCC])
        # Exactly one write opcode => a single burst.
        self.assertEqual(sum(1 for (c, m, d) in cmds
                             if m == Mode.PutX4 and d == WRITE), 1)
        # Two exit-QPI commands: the defensive one up front, and one before
        # boot so the PSRAM is left in SPI mode for the config header.
        self.assertEqual(sum(1 for (c, m, d) in cmds
                             if m == Mode.PutX4 and d == EXIT_QPI), 2)
        self.assertEqual(cmds[-1], (1, Mode.PutX4, EXIT_QPI))

    def test_tcem_chunking(self):
        """A tCEM trip mid-burst closes CS and reopens a second 0x02 burst."""
        dut = QspiRam()
        octets, reached = _collect(
            dut,
            bytes_in=[(0x000000 + i, 0x10 + i) for i in range(4)],
            tcem_at=1)  # trip in the WRITE_NEXT after the first byte

        self.assertTrue(reached, "writer never reached boot_ready")

        # Two write-opcode bursts: the original plus the post-tCEM reopen.
        write_opens = sum(1 for (c, m, d) in octets
                          if c == 1 and m == Mode.PutX4 and d == WRITE)
        self.assertEqual(write_opens, 2, "tCEM should split into two bursts")
        # At least one CS release (chip=0) appears between the bursts.
        self.assertIn(_DUMMY, octets)


if __name__ == "__main__":
    unittest.main()
