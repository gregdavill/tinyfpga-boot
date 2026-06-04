"""Run SymbiYosys against an Amaranth elaboratable.

These specs leave a module's inputs *free* (via `AnySeq`/`AnyConst`) attach
`Assert`/`Assume`/`Cover` over its interface, so sby proves the properties hold 
for every input the solver can construct, up to a bounded depth.

`verify()` emits the spec to RTLIL and the `.sby` template and runs `sby`.
Build artifacts in `formal/build/<name>/`.
"""

import shutil
import subprocess
from pathlib import Path

from amaranth.back import rtlil


BUILD = Path(__file__).resolve().parent / "build"

_SBY_TEMPLATE = """\
[tasks]
bmc
cover

[options]
bmc:   mode bmc
cover: mode cover
depth {depth}

[engines]
smtbmc yices

[script]
{script}

[files]
{files}
"""


def verify(spec, *, name, depth=20, lib_files=()):
    """Prove `spec`'s asserts (bmc) and reach its covers (cover) with sby.

    Raises SystemExit on failure so it doubles as a runnable check.
    """
    out = BUILD / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    il = rtlil.convert(spec, name="top", ports=[])
    (out / f"{name}.il").write_text(il)

    files = [f"{name}.il"]
    script = []
    for lib in lib_files:
        lib = Path(lib)
        shutil.copy(lib, out / lib.name)
        files.append(lib.name)
        script.append(f"read_verilog -lib {lib.name}")
    script += [f"read_rtlil {name}.il", "prep -top top"]

    (out / f"{name}.sby").write_text(_SBY_TEMPLATE.format(
        depth=depth, script="\n".join(script), files="\n".join(files)))

    proc = subprocess.run(["sby", "-f", f"{name}.sby"], cwd=out)
    if proc.returncode != 0:
        raise SystemExit(f"formal: {name} FAILED (sby exit {proc.returncode})")
    print(f"formal: {name} PASSED")
