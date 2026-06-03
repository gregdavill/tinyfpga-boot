# Run cocotb against the Amaranth-emitted Verilog.
#
# Usage:
#   python sim/elaborate.py   # one-time: regenerate build/sim_top.v
#   make                                       # run all cocotb tests (icarus)
#   make COCOTB_TEST_FILTER=test_enumeration   # run one test (regex match)
#   make SIM=verilator                         # untested but should drop in

SIM      ?= icarus
TOPLEVEL_LANG ?= verilog
TOPLEVEL  := sim_top

# Behavioural models for the FPGA primitives that survive elaboration are
# resolved at build time from the installed yosys.
YOSYS_DATDIR    := $(shell yosys-config --datdir 2>/dev/null)
ifeq ($(YOSYS_DATDIR),)
$(error yosys-config not found on PATH — install yosys so we can pull in cells_sim.v)
endif

# `TARGET` selects the DUT family:
#   fs (default) - iCE40 TinyFPGA BX, full-speed gateware PHY.
#   hs           - ECP5 ecpbreaker, high-speed ULPI PHY.
TARGET ?= fs

# Serial source baked into the DUT (used for elaboration on both targets, and
# for path suffixing on fs). See BoardConfig / elaborate.py.
SERIAL_SOURCE ?= flash_uid

# USB personality baked into the DUT: `uf2` (default, Mass-Storage / UF2) or
# `serial` (TinyFPGA tinyprog CDC-ACM bridge). See BoardConfig / elaborate.py.
BACKEND ?= uf2

# `AUTOBOOT=1` builds the FS DUT with auto-boot enabled (button + WEL stay
# sources) and runs the auto-boot test module.
AUTOBOOT ?= 0
ifeq ($(AUTOBOOT),1)
AUTOBOOT_FLAG := --autoboot
COCOTB_TEST_FILTER ?= test_auto_boots_into_app
export COCOTB_TEST_FILTER
else
AUTOBOOT_FLAG :=
endif

ifeq ($(TARGET),hs)
# --- High-speed (ECP5 / ULPI) ---------------------------------------------
COCOTB_TEST_MODULES ?= cocotb_tests.test_bootloader_hs
SIM_TOP_V   := $(PWD)/build/sim_top_hs.v
SIM_BUILD   := $(PWD)/build/sim_$(SIM)_hs
ELAB_BOARD  := ecpbreaker

# Self-contained behavioural models for the handful of ECP5 hard cells the
# emitted DUT instantiates (IO buffers, DDR registers, PLL). yosys' own
# ecp5/cells_sim.v can't be fed to iverilog directly (unguarded includes; the
# DDR/PLL cells are blackboxes), so we model them ourselves.
TARGET_CELLS   := $(PWD)/ecp5_cells_sim.v

else
# --- Full-speed (iCE40) ----------------------------------------------------
# The default serial source (flash_uid) keeps the original build/sim_top.v
# paths; the security_page variant and the serial backend get suffixed paths
# so they don't clobber.
ELAB_BOARD    := tinyfpga_bx
ifeq ($(AUTOBOOT),1)
COCOTB_TEST_MODULES ?= cocotb_tests.test_autoboot
SIM_TOP_V := $(PWD)/build/sim_top_autoboot.v
SIM_BUILD := $(PWD)/build/sim_$(SIM)_autoboot
else ifeq ($(BACKEND),serial)
COCOTB_TEST_MODULES ?= cocotb_tests.test_serial_bridge
SIM_TOP_V := $(PWD)/build/sim_top_serial.v
SIM_BUILD := $(PWD)/build/sim_$(SIM)_serial
else ifeq ($(SERIAL_SOURCE),flash_uid)
COCOTB_TEST_MODULES ?= cocotb_tests.test_bootloader
SIM_TOP_V := $(PWD)/build/sim_top.v
SIM_BUILD := $(PWD)/build/sim_$(SIM)
else
COCOTB_TEST_MODULES ?= cocotb_tests.test_security_serial
SIM_TOP_V := $(PWD)/build/sim_top_$(SERIAL_SOURCE).v
SIM_BUILD := $(PWD)/build/sim_$(SIM)_$(SERIAL_SOURCE)
endif

ICE40_CELLS_SIM := $(YOSYS_DATDIR)/ice40/cells_sim.v

# yosys' SB_PLL40_CORE is a `(* blackbox *)` — useless under iverilog.
# Strip it and provide our own passthrough model alongside.
CELLS_SIM_NO_PLL := $(PWD)/build/cells_sim_no_pll.v
TARGET_CELLS     := $(PWD)/ice40_pll_sim.v $(CELLS_SIM_NO_PLL)

$(CELLS_SIM_NO_PLL): $(ICE40_CELLS_SIM) | $(PWD)/build
	awk '\
	  /^\(\* blackbox \*\)\s*$$/ { pending = $$0; next } \
	  /^module SB_PLL40_CORE/    { skip = 1; pending = ""; next } \
	  skip && /^endmodule/       { skip = 0; next } \
	  skip                       { next } \
	  pending != ""              { print pending; pending = "" } \
	                             { print } \
	' $< > $@
endif

# Default target — without this, the first user-defined target below
# (the awk recipe for cells_sim_no_pll.v) wins and `make` exits without
# running cocotb.
.DEFAULT_GOAL := sim

$(PWD)/build:
	mkdir -p $@

VERILOG_SOURCES := \
	$(SIM_TOP_V) \
	$(TARGET_CELLS)

# `make WAVES=1` uses cocotb's standard FST dumper.

# `make DUMP=1` adds a verilog source as a VCD dumper.
ifeq ($(DUMP), 1)
VERILOG_SOURCES += $(PWD)/dump_flush.v
COMPILE_ARGS    += -s dump_flush
endif
COCOTB_RESULTS_FILE := $(PWD)/build/results.xml

# Wrap iverilog so we can filter out the high-volume "sorry: constant
# selects in always_* processes" notices (an iverilog 13 limitation
# with `always_comb` bit-selects — benign but noisy). cocotb invokes
# `$(CMD)` for compilation, so swapping it gives us a clean log
# without otherwise touching cocotb's makefile.
export IVERILOG_BIN := $(shell command -v iverilog)
CMD := $(PWD)/iverilog_quiet.sh

# `top` is the inner Amaranth module; icarus needs `-s sim_top` to pick
# the outer one. cocotb's Makefile.sim handles that via TOPLEVEL above.
include $(shell cocotb-config --makefiles)/Makefile.sim

# Rebuild DUT verilog
.PHONY: FORCE
$(SIM_TOP_V): FORCE | $(PWD)/build
	python elaborate.py --board $(ELAB_BOARD) --serial-source $(SERIAL_SOURCE) \
		--backend $(BACKEND) $(AUTOBOOT_FLAG) --out $@

clean::
	rm -rf build
