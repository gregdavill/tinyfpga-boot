"""Inject ASCII state-name signals for Amaranth FSM in emitted 
verilog.

This module monkey-patches `Module._pop_ctrl` to additionally emit a
combinational `<fsm>_state_name` signal alongside `<fsm>_state`,
carrying the BIG-ENDIAN ASCII bytes of the current state name

Usage: `import sim.fsm_state_names` early in `elaborate.py`. The
patch is idempotent.
"""

from __future__ import annotations

import amaranth.hdl._dsl as _dsl
from amaranth.hdl._ast import Signal, Switch


_PATCHED_ATTR = "_fsm_state_names_patched"


def install() -> None:
    """Idempotently install the FSM state-name injector."""
    if getattr(_dsl.Module, _PATCHED_ATTR, False):
        return

    orig_pop_ctrl = _dsl.Module._pop_ctrl

    def _pop_ctrl_with_state_names(self):
        # Peek at the context we're about to pop. `_pop_ctrl` calls
        # `self._ctrl_stack.pop()` as its first action, so by the time
        # the inner method returns, the data is no longer on the stack.
        fsm_data = None
        if self._ctrl_stack:
            name, data = self._ctrl_stack[-1]
            if name == "FSM":
                fsm_data = data

        result = orig_pop_ctrl(self)

        if fsm_data is None:
            return result
        # An FSM with no states has `signal = Signal(0)` and no
        # encoding worth labelling — skip it.
        fsm_signal = fsm_data.get("signal")
        encoding = fsm_data.get("encoding") or {}
        if fsm_signal is None or not encoding or len(fsm_signal) == 0:
            return result

        fsm_name = fsm_data["name"]
        max_len  = max(len(s) for s in encoding)
        name_sig = Signal(max_len * 8, name=f"{fsm_name}_state_name")

        cases = []
        state_src_locs = fsm_data.get("state_src_locs", {})
        for state_name, enc in encoding.items():
            ascii_bytes = state_name.ljust(max_len).encode("ascii", errors="replace")
            ascii_int   = int.from_bytes(ascii_bytes, byteorder="big")
            cases.append((
                enc,
                [name_sig.eq(ascii_int)],
                state_src_locs.get(state_name),
            ))

        self._top_comb_statements.append(Switch(fsm_signal, cases))
        return result

    _dsl.Module._pop_ctrl = _pop_ctrl_with_state_names
    setattr(_dsl.Module, _PATCHED_ATTR, True)
