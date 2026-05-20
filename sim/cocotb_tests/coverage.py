"""Functional coverage sampling for the bootloader cocotb regression.

A report is dumped to `sim/build/coverage_report.{yaml,txt}` by the
`_report_coverage` test
"""

from __future__ import annotations

import os
from pathlib import Path

from cocotb_coverage.coverage import (
    CoverPoint, CoverCross, coverage_db,
)


# ----------------------------------------------------------------------
# Bins
# ----------------------------------------------------------------------

# Flash opcodes the bootloader actually ISSUES on the wire. Tracking
# each as a named bin so we can tell at a glance which paths each
# regression hit.
FLASH_OPCODES = {
    "READ_UID":         0x4B,
    "PAGE_PROGRAM":     0x02,
    "SECTOR_ERASE":     0x20,
    "READ_STATUS":      0x05,
    "WRITE_ENABLE":     0x06,
}

# SCSI opcodes the device dispatches in scsi.py.
SCSI_OPCODES = {
    "TEST_UNIT_READY":  0x00,
    "REQUEST_SENSE":    0x03,
    "INQUIRY":          0x12,
    "MODE_SENSE_6":     0x1A,
    "READ_CAPACITY":    0x25,
    "READ_10":          0x28,
    "WRITE_10":         0x2A,
    "UNKNOWN":          0xFF,   # catch-all for the unknown-opcode path
}

# USB standard requests (bRequest values) the device actually
# services.
#
# SET_FEATURE (0x03) is not in LUNA's StandardRequestHandler 
USB_STANDARD_REQUESTS = {
    "GET_STATUS":           0x00,
    "CLEAR_FEATURE":        0x01,
    "SET_ADDRESS":          0x05,
    "GET_DESCRIPTOR":       0x06,
    "SET_CONFIGURATION":    0x09,
}

# Mass-Storage class requests on EP0 (bmRequestType bits 5..6 == 01).
USB_CLASS_REQUESTS = {
    "MS_GET_MAX_LUN":   0xFE,
    "MS_RESET":         0xFF,
}

# UF2 outcomes. Sampled by tests because some are intentional bug paths.
UF2_OUTCOMES = [
    "valid_block",
    "multi_block",
    "bad_start_magic",
    "bad_end_magic",
    "not_main_flash",
    "done_asserted",
]

# The QSPI Controller supports 1x/2x/4x lane modes, but the
# bootloader's flash controller only ever issues 1-lane (1-1-1) commands.
QSPI_LANES = [1]


# ----------------------------------------------------------------------
# Coverpoints
# ----------------------------------------------------------------------
#
# Each sampler is a plain function with a @CoverPoint decorator.
# Calling the function with a value that maps to one of the declared
# bins counts that bin as hit. Values that don't map to any bin do
# not contribute (which is what we want — unexpected opcodes appear
# as `0% hit` for *all* bins).

@CoverPoint(
    "top.flash.opcode",
    vname="opcode",
    bins=list(FLASH_OPCODES.values()),
    bins_labels=list(FLASH_OPCODES.keys()),
)
def cover_flash_opcode(opcode: int) -> None:
    """Sampled by `SPIFlashModel._handle_transaction` for every CS-
    framed command the DUT issues."""


@CoverPoint(
    "top.scsi.opcode",
    vname="opcode",
    bins=list(SCSI_OPCODES.values()),
    bins_labels=list(SCSI_OPCODES.keys()),
)
@CoverPoint(
    "top.scsi.cbw_dir",
    vname="dir_in",
    bins=[0, 1],
    bins_labels=["host_to_device", "device_to_host"],
)
@CoverCross(
    "top.scsi.opcode_x_dir",
    items=["top.scsi.opcode", "top.scsi.cbw_dir"],
    # A SCSI command's data direction is fixed by its definition, so
    # most (opcode x direction) cross-bins are logically impossible.
    ign_bins=[
        ("TEST_UNIT_READY", "device_to_host"),  # no data phase; CBW carries h2d default
        ("REQUEST_SENSE",   "host_to_device"),  # returns sense data → d2h only
        ("INQUIRY",         "host_to_device"),  # returns inquiry data → d2h only
        ("MODE_SENSE_6",    "host_to_device"),  # returns mode page → d2h only
        ("READ_CAPACITY",   "host_to_device"),  # returns capacity → d2h only
        ("READ_10",         "host_to_device"),  # reads sectors → d2h only
        ("WRITE_10",        "device_to_host"),  # writes sectors → h2d only
    ],
)
def cover_cbw(opcode: int, dir_in: int) -> None:
    """Sampled by tests when they ship a CBW. Records the opcode,
    direction, and the (opcode × direction) cross from a single call.
    Note: CoverCross is a *decorator* in cocotb-coverage 2.0 — it
    reads each child CoverPoint's `_new_hits` set after the
    CoverPoints fire, so it must be the innermost wrapper here."""


@CoverPoint(
    "top.usb.standard_request",
    vname="b_request",
    bins=list(USB_STANDARD_REQUESTS.values()),
    bins_labels=list(USB_STANDARD_REQUESTS.keys()),
)
def cover_usb_standard_request(b_request: int) -> None:
    """Sampled by `USBHost.control_in` / `control_out` for any
    bmRequestType with the Type field == Standard (bits 5..6 == 00)."""


@CoverPoint(
    "top.usb.class_request",
    vname="b_request",
    bins=list(USB_CLASS_REQUESTS.values()),
    bins_labels=list(USB_CLASS_REQUESTS.keys()),
)
def cover_usb_class_request(b_request: int) -> None:
    """Sampled by `USBHost.control_in` / `control_out` for class-type
    requests (bmRequestType bits 5..6 == 01)."""


@CoverPoint(
    "top.qspi.lanes",
    vname="lanes",
    bins=QSPI_LANES,
)
def cover_qspi_lanes(lanes: int) -> None:
    """Sampled by `SPIFlashModel._shift_in` / `_shift_out` whenever it
    samples DQ in a given lane mode."""


@CoverPoint(
    "top.uf2.outcome",
    vname="outcome",
    bins=UF2_OUTCOMES,
)
def cover_uf2_outcome(outcome: str) -> None:
    """Sampled explicitly by UF2 tests. The decoder is internal to the
    DUT and its outcome (valid / bad-magic / skipped / done) isn't
    visible on the bus — the test knows what it sent."""


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

REPORT_DIR = Path(__file__).resolve().parent.parent / "build"


def report_paths() -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR / "coverage_report.yaml", REPORT_DIR / "coverage_report.txt"


def _is_coverpoint(name: str, item) -> bool:
    """Identify the leaf CoverPoint / CoverCross entries that have a
    flat bin → int mapping. The DB also surfaces parent aggregators
    (one dot) and individual bin entries (no `top.` prefix); skip both."""
    if not name.startswith("top."):
        return False
    detail = getattr(item, "detailed_coverage", None)
    if not isinstance(detail, dict) or not detail:
        return False
    # CoverPoint detail values are integers (per-bin hit counts).
    return all(isinstance(v, int) for v in detail.values())


def dump_reports() -> None:
    """Emit YAML + a small text summary. Called from `_report_coverage`
    at end of regression."""
    yaml_path, txt_path = report_paths()
    coverage_db.export_to_yaml(str(yaml_path))

    lines = ["# bootloader functional coverage", ""]
    total_size = 0
    total_hit  = 0
    for name, item in sorted(coverage_db.items()):
        if not _is_coverpoint(name, item):
            continue
        size = item.size
        hit  = item.coverage
        total_size += size
        total_hit  += hit
        pct = (100.0 * hit / size) if size else 0.0
        lines.append(f"{name:36s} {hit:>3d} / {size:<3d}  ({pct:5.1f}%)")
        for bin_label, bin_hits in item.detailed_coverage.items():
            marker = "✓" if bin_hits else "·"
            label  = repr(bin_label) if not isinstance(bin_label, str) else bin_label
            lines.append(f"    {marker} {label:<32s}  {bin_hits}")
        lines.append("")
    total_pct = (100.0 * total_hit / total_size) if total_size else 0.0
    lines.append(f"TOTAL: {total_hit} / {total_size}  ({total_pct:.1f}%)")
    txt_path.write_text("\n".join(lines) + "\n")

    # Also echo to stdout so the regression log is self-contained.
    print()
    print("=" * 72)
    print("functional coverage")
    print("=" * 72)
    print(txt_path.read_text())
