"""Emit `build/sim_top.v` from `Top` for cocotb.
"""

import pathlib
import sys

from amaranth.hdl import Fragment
from amaranth.hdl._xfrm import DomainLowerer
from amaranth.lib import io
from amaranth.back import rtlil, verilog


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from top import Top                       # noqa: E402
from build import BoardConfig                    # noqa: E402
from sim.cocotb_platform import CocotbPlatform  # noqa: E402
from sim import fsm_state_names                  # noqa: E402

# Patch Amaranth so every `m.FSM()` also exposes an ASCII-encoded
# state-name signal in the generated Verilog. Must run before Top()
# is instantiated.
fsm_state_names.install()


def emit(platform, design, *, name: str = "sim_top", emit_src: bool = False) -> str:
    """Replicates the un-toolchained part of `Platform.prepare()` and
    returns Verilog text."""
    fragment = Fragment.get(design, platform)
    fragment._propagate_domains(platform.create_missing_domain, platform=platform)
    fragment = DomainLowerer()(fragment)

    # Each buffered `platform.request()` recorded a (pin, port, buffer)
    # triple; lower the buffer to a subfragment and splice it in.
    for pin, port, buffer in platform.iter_pins():
        buf = Fragment.get(buffer, platform)
        buf._propagate_domains(lambda _name: None)
        buf = DomainLowerer()(buf)
        fragment.add_subfragment(buf, name=f"pin_{pin.name}")

    # Collect every IOPort that should surface at the Verilog boundary:
    # buffered pins live behind `port.io` (or `.p`/`.n` for differential);
    # `dir="-"` ports we track separately on the platform.
    ports = []
    for _pin, port, _buffer in platform.iter_pins():
        if isinstance(port, io.SingleEndedPort):
            ports.append(port.io)
        elif isinstance(port, io.DifferentialPort):
            ports.extend([port.p, port.n])
    ports.extend(getattr(platform, "_raw_ports", ()))

    # `propagate_domains=False` - we already ran platform's 
    # `create_missing_domain`
    rtlil_text, _name_map = rtlil.convert_fragment(
        fragment, ports=ports, name=name, emit_src=emit_src,
        propagate_domains=False,
    )
    # `write_verilog_opts=("-sv",)` - without `-sv`, yosys emits plain
    # `always @*` for combinational logic. iverilog 13 does not
    # evaluate those blocks at t=0 when none of their sensitivity
    # signals change from their initial values.
    text = verilog._convert_rtlil_text(
        rtlil_text, write_verilog_opts=("-sv",),
    )
    return text


def main():
    platform = CocotbPlatform()

    # Use a sim-only config that mostly just shortens the warmboot
    # idle window so the BOOT pulse fires inside our per-test sim-time
    # budget. Hardware builds use the platform defaults ~50 ms.
    sim_config = BoardConfig(
        platform="tinyfpga_bx",
        vid=0x1209, pid=0x5AF0,
        manufacturer="TinyFPGA", product="Bootloader",
        board_id="TinyFPGA-BX-v1", model="TinyFPGA BX",
        url="https://tinyfpga.com",
        scsi_vendor="TINYFPGA", scsi_product="UF2 Bootloader",
        reload_slot=1,
        # ~85 µs at 12 MHz
        reload_idle_cycles=1000,
    )
    top = Top(sim_config)

    text = emit(platform, top)

    out_dir = ROOT / "sim" / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sim_top.v"
    out_path.write_text(text)
    print(f"wrote {out_path} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
