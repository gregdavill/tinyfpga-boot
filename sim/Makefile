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
COCOTB_TEST_MODULES := cocotb_tests.test_bootloader

# Default target — without this, the first user-defined target below
# (the awk recipe for cells_sim_no_pll.v) wins and `make` exits without
# running cocotb.
.DEFAULT_GOAL := sim

# Behavioural models for the iCE40 primitives that survived elaboration
# (SB_PLL40_CORE, SB_IO, SB_DFF). Resolved at build time from the
# installed yosys.
YOSYS_DATDIR    := $(shell yosys-config --datdir 2>/dev/null)
ifeq ($(YOSYS_DATDIR),)
$(error yosys-config not found on PATH — install yosys so we can pull in ice40/cells_sim.v)
endif
ICE40_CELLS_SIM := $(YOSYS_DATDIR)/ice40/cells_sim.v

# yosys' SB_PLL40_CORE is a `(* blackbox *)` — useless under iverilog.
# Strip it and provide our own passthrough model alongside.
CELLS_SIM_NO_PLL := $(PWD)/build/cells_sim_no_pll.v

$(CELLS_SIM_NO_PLL): $(ICE40_CELLS_SIM) | $(PWD)/build
	awk '\
	  /^\(\* blackbox \*\)\s*$$/ { pending = $$0; next } \
	  /^module SB_PLL40_CORE/    { skip = 1; pending = ""; next } \
	  skip && /^endmodule/       { skip = 0; next } \
	  skip                       { next } \
	  pending != ""              { print pending; pending = "" } \
	                             { print } \
	' $< > $@

$(PWD)/build:
	mkdir -p $@

VERILOG_SOURCES := \
	$(PWD)/build/sim_top.v \
	$(PWD)/ice40_pll_sim.v \
	$(CELLS_SIM_NO_PLL)

# `make WAVES=1` uses cocotb's standard FST dumper.

# `make DUMP=1` adds a verilog source as a VCD dumper.
ifeq ($(DUMP), 1)
VERILOG_SOURCES += $(PWD)/dump_flush.v
COMPILE_ARGS    += -s dump_flush
endif
COCOTB_RESULTS_FILE := $(PWD)/build/results.xml
SIM_BUILD := $(PWD)/build/sim_$(SIM)

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

# Convenience: emit Verilog as part of `make`.
$(PWD)/build/sim_top.v: elaborate.py cocotb_platform.py ../top.py
	python elaborate.py

clean::
	rm -rf build
