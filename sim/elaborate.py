"""Emit `build/sim_top.v` from `Top` for cocotb.

Two targets, selected with `--board`:

* `tinyfpga_bx` (default) - the iCE40 full-speed DUT on `CocotbPlatform`.
* `ecpbreaker` - the ECP5 high-speed (ULPI) DUT on `CocotbHSPlatform`. The two
  LUNA high-speed timers that would otherwise dominate sim time (the 2 ms
  device chirp and the 1 ms ULPI Tstart) are shortened, in the same spirit as
  the FS build's `reload_idle_cycles` shortening.
"""

import argparse
import dataclasses
import os
import pathlib
import sys

from amaranth.hdl import Fragment
from amaranth.hdl._xfrm import DomainLowerer
from amaranth.lib import io
from amaranth.back import rtlil, verilog


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from top import Top                       # noqa: E402
from config import BoardConfig, SerialSource, Backend, SLOT1_OFFSET  # noqa: E402
import config as _config                  # noqa: E402
from sim.cocotb_platform import CocotbPlatform  # noqa: E402
from sim.cocotb_hs_platform import CocotbHSPlatform  # noqa: E402
from sim import fsm_state_names                  # noqa: E402

# Patch Amaranth so every `m.FSM()` also exposes an ASCII-encoded
# state-name signal in the generated Verilog. Must run before Top()
# is instantiated.
fsm_state_names.install()


def shorten_hs_timers(device_chirp_cycles=240, tstart_cycles=240,
                      hs_reset_cycles=600, detect_suspend_cycles=200):
    """Shrink LUNA's big high-speed timers so HS enumeration - and, crucially,
    re-resetting an already-high-speed device between tests - fits a sim-time
    budget. The host PHY model keys off bus events (device chirp start/end,
    line state), not these absolute durations, so shortening them doesn't
    change the handshake the testbench has to drive.

    Must run before `Top()` (and thus the LUNA submodules) is built.
    """
    from luna.gateware.usb.usb2.reset import USBResetSequencer
    from luna.gateware.interface.ulpi import UTMITranslator
    # DEVICE_CHIRP holds for _CYCLES_2_MILLISECONDS; Tstart gates the ULPI bus
    # for _CYCLES_1_MILLISECONDS.
    USBResetSequencer._CYCLES_2_MILLISECONDS = device_chirp_cycles
    UTMITranslator._CYCLES_1_MILLISECONDS = tstart_cycles
    # From HS_NON_RESET an SE0 of _CYCLES_3_MILLISECONDS drops to FS, then
    # DETECT_HS_SUSPEND waits _CYCLES_200_MICROSECONDS before re-running
    # high-speed detection. Shorten both so an inter-test reset re-chirps fast.
    # (These also size the `timer`/`line_state_time` counters; keep them well
    # above the chirp constants 240 / 150 that are still compared against them.)
    USBResetSequencer._CYCLES_3_MILLISECONDS = hs_reset_cycles
    USBResetSequencer._CYCLES_200_MICROSECONDS = detect_suspend_cycles


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


def _fs_sim_config(serial_source, backend, autoboot=False):
    """Full-speed (TinyFPGA BX) sim config. Mostly just shortens the warmboot
    idle window so the BOOT pulse fires inside our per-test sim-time budget;
    hardware builds use the platform defaults (~50 ms).

    `autoboot=True` enables the auto-boot decision with the button + WEL stay
    sources (the sim platform grows a `button` resource to match)."""
    stay = ()
    if autoboot:
        from staysource import ButtonStaySource, WriteEnableStaySource
        stay = (ButtonStaySource, WriteEnableStaySource)
    return BoardConfig(
        name="sim",
        platform=CocotbPlatform,
        vid=0x1209, pid=0x5AF0,
        manufacturer="TinyFPGA",
        board_id="TinyFPGA-BX-v1", model="TinyFPGA BX",
        url="https://tinyfpga.com",
        scsi_vendor="TINYFPGA", scsi_product="UF2 Bootloader",
        serial_source=SerialSource(serial_source),
        backend=Backend(backend),
        reload_slot=1,
        reload_image_offset=SLOT1_OFFSET,
        # ~85 µs at 12 MHz
        reload_idle_cycles=1000,
        stay_sources=stay,
    )


def _hs_sim_config(serial_source, backend):
    """High-speed (ecpbreaker) sim config: the real board config retargeted to
    the sim platform, with a short reload-idle window (cycles are 60 MHz)."""
    return dataclasses.replace(
        _config.ecpbreaker,
        name="sim_hs",
        platform=CocotbHSPlatform,
        serial_source=SerialSource(serial_source),
        backend=Backend(backend),
        reload_idle_cycles=2000,
        stay_sources=(),
    )


def main():
    parser = argparse.ArgumentParser(description="Emit sim_top.v for cocotb")
    parser.add_argument(
        "--board",
        default="tinyfpga_bx",
        choices=["tinyfpga_bx", "ecpbreaker"],
        help="which DUT to elaborate (default: tinyfpga_bx, full-speed)",
    )
    parser.add_argument(
        "--serial-source",
        default=os.environ.get("SERIAL_SOURCE", "flash_uid"),
        choices=["flash_uid", "security_page"],
        help="USB serial source baked into the DUT (default: flash_uid)",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("BACKEND", "uf2"),
        choices=[b.value for b in Backend],
        help="USB personality baked into the DUT (default: uf2)",
    )
    parser.add_argument(
        "--autoboot", action="store_true",
        default=os.environ.get("AUTOBOOT", "0") == "1",
        help="enable the auto-boot decision with button + WEL stay sources (FS only)",
    )
    parser.add_argument("--out", default=None,
                        help="output Verilog path (default: sim/build/sim_top.v)")
    args = parser.parse_args()

    if args.board == "ecpbreaker":
        shorten_hs_timers()
        platform = CocotbHSPlatform()
        sim_config = _hs_sim_config(args.serial_source, args.backend)
        default_out = ROOT / "sim" / "build" / "sim_top_hs.v"
    else:
        platform = CocotbPlatform()
        sim_config = _fs_sim_config(args.serial_source, args.backend, autoboot=args.autoboot)
        default_out = ROOT / "sim" / "build" / "sim_top.v"

    top = Top(sim_config)

    text = emit(platform, top)

    out_path = pathlib.Path(args.out) if args.out else default_out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    print(f"wrote {out_path} (board={args.board}, backend={args.backend}, "
          f"serial_source={args.serial_source}, {len(text)} bytes)")


if __name__ == "__main__":
    main()
