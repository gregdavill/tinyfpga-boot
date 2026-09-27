from amaranth.sim import Simulator


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


def simulate(dut, *testbenches, vcd=None):
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    for tb in testbenches:
        sim.add_testbench(tb)
    if vcd:
        with sim.write_vcd(vcd):
            sim.run()
    else:
        sim.run()
