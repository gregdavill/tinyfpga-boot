from amaranth import *
from amaranth.lib.wiring import In, Out
from amaranth.lib import wiring
from amaranth.lib import stream, data


UID_CMD = 0x4B

class FlashUID(wiring.Component):
    def __init__(self, uid_bytes = 8, dummy_bytes = 4):
        self._uid_bytes = uid_bytes
        self._dummy_bytes = dummy_bytes
        super().__init__({
            # FLASH QSPI interface
            'o': Out(stream.Signature(data.StructLayout({
                "chip": range(2),
                "mode": Mode,
                "data": 8
            }))),
            'i': In(stream.Signature(data.StructLayout({
                "data": 8
            }))),

            # Request
            'req': In(1),
            
            # UUID output
            'valid': Out(1),
            'uuid': Out(uid_bytes * 8)
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        write_count = Signal(range(1 + self._uid_bytes + self._dummy_bytes), init=(self._uid_bytes + self._dummy_bytes - 1))
        read_count = Signal(range(1 + self._uid_bytes + self._dummy_bytes), init=(self._uid_bytes + self._dummy_bytes - 1))

        write_done = Signal()

        # Write Path (OP + DUMMY + UUID)
        with m.FSM():
            with m.State('IDLE'):
                m.d.comb += [
                    # Send UUID command to QSPI
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.PutX1),
                    self.o.p.data.eq(UID_CMD),
                    self.o.valid.eq(self.req),
                ]
                with m.If(self.req & self.o.ready):
                    m.next = 'READ'

            with m.State('READ'):
                m.d.comb += [
                    # Request dummy + UUID bytes from SPI
                    self.o.p.chip.eq(1),
                    self.o.p.mode.eq(Mode.GetX1),
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.d.sync += write_count.eq(write_count - 1)
                    with m.If(write_count == 0):
                        m.next = 'RELEASE'

            with m.State('RELEASE'):
                m.d.comb += [
                    # Release CS line
                    self.o.p.chip.eq(0),
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.next = 'DONE'

            with m.State('DONE'):
                ...

        # Read Path (Capture DUMMY then UUID)
        with m.FSM() as fsm:
            m.d.comb += self.valid.eq(fsm.ongoing('DONE'))

            with m.State('READ'):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    m.d.sync += self.uuid.eq(Cat(self.i.p.data[:8], self.uuid[:-8]))
                    m.d.sync += read_count.eq(read_count - 1)
                    with m.If(read_count == 0):
                        m.next = 'DONE'

            with m.State('DONE'):
                ...

        return m


# Test cases
import unittest
from random import randint
from amaranth.sim import Simulator

from .qspi import Controller, PortGroup, Mode
from amaranth.lib import io

async def stream_get(ctx, stream):
    ctx.set(stream.ready, 1)
    payload, = await ctx.tick().sample(stream.payload).until(stream.valid)
    ctx.set(stream.ready, 0)
    return payload

async def stream_put(ctx, stream, payload):
    ctx.set(stream.valid, 1)
    ctx.set(stream.payload, payload)
    await ctx.tick().until(stream.ready)
    ctx.set(stream.valid, 0)

class TestStride(unittest.TestCase):
    class DUT(Elaboratable):
        def __init__(self, pads):
            self._pads = pads
            self.uuid_i = None
            super().__init__()

        def elaborate(self, platform) -> Module:
            m = Module()

            
            m.submodules.uuid = uuid = FlashUID()
            m.submodules.qspi = qspi = Controller(self._pads)

            wiring.connect(m, uuid.o, qspi.i)
            wiring.connect(m, uuid.i, qspi.o)

            self.uuid_i = uuid.i
            self.qspi_o = qspi.o
            self.uuid = uuid.uuid
            self.valid = uuid.valid

            m.d.sync += uuid.req.eq(1)
            m.d.comb += qspi.divisor.eq(1)
 
            return m

    def test_basic(self):
        pads = PortGroup(
            sck=io.SimulationPort("o", 1),
            cs=io.SimulationPort("o", 1),
            io=io.SimulationPort("io", 4)
        )


        dut = self.DUT(pads)

        async def generator(ctx):
            await ctx.tick().repeat(3)
            await ctx.tick().repeat(500)

            
        uuid = [
            0x00, # dummy 
            0x00, # dummy 
            0x00, # dummy 
            0x11, 
            0x22, 
            0x33, 
            0x44,
            0x55,
            0x66,
            0x77,
            0x88,
        ]

        async def uuid_inject(ctx):
            for u in uuid:
                v = await stream_get(ctx, dut.qspi_o)
                # await ctx.tick().until(dut.qspi_o.valid)
                await stream_put(ctx, dut.uuid_i, {'data' : u})
            await ctx.tick().sample(dut.uuid).until(dut.valid)

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(generator)
        # sim.add_testbench(uuid_inject)
        # sim.run()
        with sim.write_vcd("uuid.vcd"):
            sim.run()