"""APS6404L QSPI-PSRAM writer.

Streams image into a QSPI PSRAM. This is faster than FLASH with erase/program
cycles.

Differs from `QspiFlash`:

  * PSRAM is directly writable, no WREN, no erase, no status poll.
  * Writes go out as QPI bursts for speed.
  * The PSRAM enforces a maximum CS-active time (tCEM, 8 us).

"""

from amaranth import *
from amaranth.lib import wiring, stream, data
from amaranth.lib.wiring import In, Out

from .qspi import Mode


EXIT_QPI  = 0xF5   # QPI -> SPI (sent quad; defensive, see QPI_EXIT)
ENTER_QPI = 0x35   # SPI -> QPI (sent single-line)
WRITE     = 0x02   # PSRAM write (QPI: command + addr + data all x4)


class QspiRam(wiring.Component):
    def __init__(self):
        super().__init__({
            # Write addr/data interface (from the UF2 decoder)
            "i":    In(stream.Signature(data.StructLayout({"addr": 24, "data": 8}))),

            # QSPI bus
            "qo":   Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "qi":   In(stream.Signature(data.StructLayout({"data": 8}))),

            # Flush - transfer complete; close the final burst then boot-arm.
            "done": In(1),

            # Bank control / tCEM handshake with DualBank.
            "bank":         Out(1),   # 0 = FLASH, 1 = RAM  -> cfg_ctrl
            "cs_open":      Out(1),   # high while CS asserted -> tCEM counter
            "tcem_expired": In(1),    # break the burst before tCEM
            "boot_ready":   Out(1),   # boot-arm done -> gate reconfigure
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        addr_latch = Signal(24)   # address of the byte in data_latch
        data_latch = Signal(8)
        cur_addr   = Signal(24)   # address of the last byte written
        addr_count = Signal(range(3))

        target_bank = Signal()    # held cfg_ctrl value
        m.d.comb += self.bank.eq(target_bank)

        # The writer never issues Get*; drain the read side so the bus mux is
        # never back-pressured.
        m.d.comb += self.qi.ready.eq(1)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.qo.p.chip.eq(chip),
                self.qo.p.mode.eq(mode),
                self.qo.p.data.eq(d),
                self.qo.valid.eq(1),
            ]

        # Selected address byte (MSB first), shared by WRITE_ADDR.
        addr_byte = Signal(8)
        with m.Switch(addr_count):
            with m.Case(0):
                m.d.comb += addr_byte.eq(addr_latch[16:24])
            with m.Case(1):
                m.d.comb += addr_byte.eq(addr_latch[8:16])
            with m.Default():
                m.d.comb += addr_byte.eq(addr_latch[0:8])

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += [
                        addr_latch.eq(self.i.p.addr),
                        data_latch.eq(self.i.p.data),
                        target_bank.eq(1),        # point the bus at RAM
                    ]
                    m.next = "SWAP_ASSERT"

            # --- One throwaway CS pulse latches cfg_ctrl=RAM (bus -> PSRAM) ---
            with m.State("SWAP_ASSERT"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "SWAP_RELEASE"

            with m.State("SWAP_RELEASE"):
                send(0, Mode.Dummy)               # CS rising -> latch RAM
                with m.If(self.qo.ready):
                    m.next = "QPI_EXIT"

            # --- Defensive exit-QPI: the PSRAM may still be in QPI from a prior
            # bootloader entry (no power cycle), where the single-line 0x35
            # below would be misread. Sent x4 so a QPI chip decodes it and
            # returns to SPI; an SPI chip sees a truncated command and ignores
            # it on CS release. Either way we then enter QPI from a known state.
            with m.State("QPI_EXIT"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX4, EXIT_QPI)
                with m.If(self.qo.ready):
                    m.next = "QPI_EXIT_RELEASE"

            with m.State("QPI_EXIT_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "QPI_ENTER"

            # --- Enter QPI mode (0x35, single line) ---
            with m.State("QPI_ENTER"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX1, ENTER_QPI)
                with m.If(self.qo.ready):
                    m.next = "QPI_RELEASE"

            with m.State("QPI_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "WRITE_OPEN"

            # --- Open a write burst: 0x02 + 24-bit address, all x4 ---
            with m.State("WRITE_OPEN"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX4, WRITE)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "WRITE_ADDR"

            with m.State("WRITE_ADDR"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX4, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.next = "WRITE_DATA"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("WRITE_DATA"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX4, data_latch)
                with m.If(self.qo.ready):
                    m.d.sync += cur_addr.eq(addr_latch)
                    m.next = "WRITE_NEXT"

            with m.State("WRITE_NEXT"):
                m.d.comb += self.cs_open.eq(1)
                with m.If(self.tcem_expired):
                    # CS budget spent: close, then reopen at the next address.
                    m.next = "TCEM_CLOSE"
                with m.Else():
                    m.d.comb += self.i.ready.eq(1)
                    with m.If(self.i.valid):
                        m.d.sync += [
                            addr_latch.eq(self.i.p.addr),
                            data_latch.eq(self.i.p.data),
                        ]
                        with m.If(self.i.p.addr == cur_addr + 1):
                            # Contiguous: keep bursting (PSRAM auto-increments).
                            m.next = "WRITE_DATA"
                        with m.Else():
                            m.next = "REOPEN_CLOSE"
                    with m.Elif(self.done):
                        m.next = "DONE_CLOSE"

            # tCEM forced a close: CS released (counter resets), wait for the
            # next byte, then reopen a fresh burst at its address.
            with m.State("TCEM_CLOSE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "TCEM_WAIT"

            with m.State("TCEM_WAIT"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += [
                        addr_latch.eq(self.i.p.addr),
                        data_latch.eq(self.i.p.data),
                    ]
                    m.next = "WRITE_OPEN"
                with m.Elif(self.done):
                    m.next = "RAM_EXIT_QPI"       # CS already closed

            # Address jump: close this burst, reopen at the new address.
            with m.State("REOPEN_CLOSE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "WRITE_OPEN"

            with m.State("DONE_CLOSE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "RAM_EXIT_QPI"

            # --- Leave the PSRAM in SPI mode before reconfigure: the ECP5
            # config header (run from FLASH) is what re-enables quad on the
            # RAM. The bus is still pointed at the RAM here (bank=RAM). ---
            with m.State("RAM_EXIT_QPI"):
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.PutX4, EXIT_QPI)
                with m.If(self.qo.ready):
                    m.next = "RAM_EXIT_RELEASE"

            with m.State("RAM_EXIT_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "ARM_FLASH_ASSERT"

            # --- Boot-arm: latch FLASH (one pulse), then hold RAM quiet ---
            with m.State("ARM_FLASH_ASSERT"):
                m.d.sync += target_bank.eq(0)     # drive cfg_ctrl=FLASH
                m.d.comb += self.cs_open.eq(1)
                send(1, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.next = "ARM_FLASH_RELEASE"

            with m.State("ARM_FLASH_RELEASE"):
                send(0, Mode.Dummy)               # CS rising -> latch FLASH
                with m.If(self.qo.ready):
                    m.next = "ARM_RAM"

            with m.State("ARM_RAM"):
                # cfg_ctrl=RAM with no CS pulse: the reconfigure's first config
                # CS edge latches RAM. Hold here; boot_ready arms the reload.
                m.d.sync += target_bank.eq(1)
                m.d.comb += self.boot_ready.eq(1)

        return m


# Test cases
import unittest
from .test_util import simulate


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
