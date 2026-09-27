from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data, enum
from .controller import Mode


WRITE_ENABLE = 0x06
SECTOR_ERASE = 0x20  # 4 KiB sector erase (used by boot_header)
BLOCK_ERASE  = 0xD8  # 64 KiB block erase
PAGE_PROGRAM = 0x02
READ_STATUS  = 0x05


class PollReturn(enum.Enum, shape=2):
    WREN_ERASE = 0
    WREN_WRITE = 1
    IDLE       = 2


#: The write side a flash-writing backend drives, and a `QspiFlash` /
#: `DualBankWriter` backing consumes.
FlashPort = wiring.Signature({
    "w":          Out(stream.Signature(data.StructLayout({"addr": 24, "data": 8}))),
    "flush":      Out(1),   # triggers final page close + poll
    "ram_select": Out(1),   # route to PSRAM (dual-bank only)
})


class QspiFlash(wiring.Component):
    def __init__(self):
        super().__init__({
            # Write side (addr/data stream + flush); `ram_select` is ignored.
            "port": In(FlashPort),

            # FLASH QSPI interface
            "qo":   Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8,
            }))),
            "qi":   In(stream.Signature(data.StructLayout({
                "data": 8,
            }))),

            # Backing status.
            "arm":        Out(1),   # transfer complete → ready to reboot
            "active":     Out(1),   # driving the QSPI bus (writing)
            "cfg_ctrl_o": Out(1),   # bank latch — single-bank is always FLASH (0)
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        # Write stream in, backing status out.
        i = self.port.w
        m.d.comb += [
            self.arm.eq(self.port.flush),   # single-bank: flush ⇒ ready to reboot
            self.active.eq(self.qo.valid),
            self.cfg_ctrl_o.eq(0),
        ]

        # Latched input
        addr_latch = Signal(24)
        data_latch = Signal(8)

        # Block/page tracking. 
        # erase unit is a 64 KiB block (addr[23:16]).
        # pages are 256 bytes (addr[23:8]).
        current_block  = Signal(8)   # addr[23:16]
        current_page   = Signal(16)  # addr[23:8]
        block_valid    = Signal()

        # Address byte counter (counts 0, 1, 2)
        addr_count = Signal(range(3))

        # FSM routing signals
        wren_target = Signal()  # 0=ERASE_CMD, 1=WRITE_CMD
        poll_return = Signal(PollReturn)

        # Whether a new byte was latched during WRITE_NEXT
        has_next = Signal()

        # Latched status register
        status = Signal(8)

        def send(chip, mode, d=0):
            m.d.comb += [
                self.qo.p.chip.eq(chip),
                self.qo.p.mode.eq(mode),
                self.qo.p.data.eq(d),
                self.qo.valid.eq(1),
            ]

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += i.ready.eq(1)
                with m.If(i.valid):
                    m.d.sync += [
                        addr_latch.eq(i.p.addr),
                        data_latch.eq(i.p.data),
                    ]
                    with m.If(~block_valid | (i.p.addr[16:24] != current_block)):
                        # New block — erase first
                        m.d.sync += wren_target.eq(0)  # ERASE
                        m.next = "WREN"
                    with m.Else():
                        # Same block — write directly
                        m.d.sync += wren_target.eq(1)  # WRITE
                        m.next = "WREN"


            with m.State("WREN"):
                send(1, Mode.PutX1, WRITE_ENABLE)
                with m.If(self.qo.ready):
                    m.next = "WREN_RELEASE"

            with m.State("WREN_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(wren_target == 0):
                        m.next = "ERASE_CMD"
                    with m.Else():
                        m.next = "WRITE_CMD"


            with m.State("ERASE_CMD"):
                send(1, Mode.PutX1, BLOCK_ERASE)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "ERASE_ADDR"

            with m.State("ERASE_ADDR"):
                # Send 3 address bytes (block-aligned, so low 16 bits = 0):
                # byte 0 = addr[23:16], bytes 1 and 2 = 0x00.
                addr_byte = Signal(8)
                with m.Switch(addr_count):
                    with m.Case(0):
                        m.d.comb += addr_byte.eq(addr_latch[16:24])
                    with m.Default():
                        m.d.comb += addr_byte.eq(0)
                send(1, Mode.PutX1, addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.next = "ERASE_RELEASE"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("ERASE_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    m.d.sync += [
                        current_block.eq(addr_latch[16:24]),
                        block_valid.eq(1),
                        poll_return.eq(PollReturn.WREN_WRITE),
                    ]
                    m.next = "POLL_CMD"


            with m.State("WRITE_CMD"):
                send(1, Mode.PutX1, PAGE_PROGRAM)
                with m.If(self.qo.ready):
                    m.d.sync += addr_count.eq(0)
                    m.next = "WRITE_ADDR"

            with m.State("WRITE_ADDR"):
                w_addr_byte = Signal(8)
                with m.Switch(addr_count):
                    with m.Case(0):
                        m.d.comb += w_addr_byte.eq(addr_latch[16:24])
                    with m.Case(1):
                        m.d.comb += w_addr_byte.eq(addr_latch[8:16])
                    with m.Default():
                        m.d.comb += w_addr_byte.eq(addr_latch[0:8])
                send(1, Mode.PutX1, w_addr_byte)
                with m.If(self.qo.ready):
                    with m.If(addr_count == 2):
                        m.d.sync += current_page.eq(addr_latch[8:24])
                        m.next = "WRITE_DATA"
                    with m.Else():
                        m.d.sync += addr_count.eq(addr_count + 1)

            with m.State("WRITE_DATA"):
                send(1, Mode.PutX1, data_latch)
                with m.If(self.qo.ready):
                    m.next = "WRITE_NEXT"

            with m.State("WRITE_NEXT"):
                m.d.comb += i.ready.eq(1)
                with m.If(i.valid):
                    m.d.sync += [
                        addr_latch.eq(i.p.addr),
                        data_latch.eq(i.p.data),
                        has_next.eq(1),
                    ]
                    with m.If(i.p.addr[8:24] == current_page):
                        # Same page — continue writing
                        m.next = "WRITE_DATA"
                    with m.Else():
                        # Different page — close this page
                        m.next = "WRITE_RELEASE"
                with m.Elif(self.port.flush):
                    m.d.sync += has_next.eq(0)
                    m.next = "WRITE_RELEASE"

            with m.State("WRITE_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(~has_next):
                        # Done signal caused release
                        m.d.sync += poll_return.eq(PollReturn.IDLE)
                    with m.Elif(addr_latch[16:24] != current_block):
                        # New block
                        m.d.sync += poll_return.eq(PollReturn.WREN_ERASE)
                    with m.Else():
                        # New page, same block
                        m.d.sync += poll_return.eq(PollReturn.WREN_WRITE)
                    m.next = "POLL_CMD"


            with m.State("POLL_CMD"):
                send(1, Mode.PutX1, READ_STATUS)
                with m.If(self.qo.ready):
                    m.next = "POLL_READ"

            with m.State("POLL_READ"):
                send(1, Mode.GetX1, 0)
                with m.If(self.qo.ready):
                    m.next = "POLL_CAPTURE"

            with m.State("POLL_CAPTURE"):
                m.d.comb += self.qi.ready.eq(1)
                with m.If(self.qi.valid):
                    m.d.sync += status.eq(self.qi.p.data)
                    m.next = "POLL_RELEASE"

            with m.State("POLL_RELEASE"):
                send(0, Mode.Dummy)
                with m.If(self.qo.ready):
                    with m.If(status[0]):
                        # WIP=1, retry
                        m.next = "POLL_CMD"
                    with m.Else():
                        # WIP=0, done
                        with m.Switch(poll_return):
                            with m.Case(PollReturn.WREN_ERASE):
                                m.d.sync += wren_target.eq(0)
                                m.next = "WREN"
                            with m.Case(PollReturn.WREN_WRITE):
                                m.d.sync += wren_target.eq(1)
                                m.next = "WREN"
                            with m.Case(PollReturn.IDLE):
                                m.d.sync += block_valid.eq(0)
                                m.next = "IDLE"

        return m
