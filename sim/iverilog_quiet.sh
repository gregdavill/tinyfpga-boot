#!/usr/bin/env bash
# Wrap iverilog and drop the "sorry: constant selects in always_*
# processes are not fully supported" lines from stderr.
#
# `IVERILOG_BIN` is the real iverilog binary, set from the Makefile.
exec "${IVERILOG_BIN:-iverilog}" "$@" \
    2> >(grep --line-buffered -v 'sorry: constant selects in always_\* processes' >&2)
