// Behavioural SB_PLL40_CORE for cocotb simulation.
//
// cocotb is expected to drive the reference clock at the
// post-PLL frequency
//
// The Makefile strips SB_PLL40_CORE out of yosys' cells_sim.v before
// handing the rest to iverilog; the remaining cells (SB_IO, SB_DFF,
// SB_LUT4, etc.) keep their upstream behaviour.

`timescale 1ns / 1ps

module SB_PLL40_CORE (
    input        REFERENCECLK,
    output       PLLOUTCORE,
    output       PLLOUTGLOBAL,
    input        EXTFEEDBACK,
    input  [7:0] DYNAMICDELAY,
    output reg   LOCK,
    input        BYPASS,
    input        RESETB,
    input        LATCHINPUTVALUE,
    output       SDO,
    input        SDI,
    input        SCLK
);
    parameter FEEDBACK_PATH = "SIMPLE";
    parameter DELAY_ADJUSTMENT_MODE_FEEDBACK = "FIXED";
    parameter DELAY_ADJUSTMENT_MODE_RELATIVE = "FIXED";
    parameter SHIFTREG_DIV_MODE = 1'b0;
    parameter FDA_FEEDBACK = 4'b0000;
    parameter FDA_RELATIVE = 4'b0000;
    parameter PLLOUT_SELECT = "GENCLK";
    parameter DIVR = 4'b0000;
    parameter DIVF = 7'b0000000;
    parameter DIVQ = 3'b000;
    parameter FILTER_RANGE = 3'b000;
    parameter ENABLE_ICEGATE = 1'b0;
    parameter TEST_MODE = 1'b0;
    parameter EXTERNAL_DIVIDE_FACTOR = 1;

    assign PLLOUTCORE   = REFERENCECLK;
    assign PLLOUTGLOBAL = REFERENCECLK;
    assign SDO = 1'b0;

    initial begin
        LOCK = 1'b0;
        // ~1 µs lock delay so we can observe the reset stretch as well.
        #16000 LOCK = 1'b1;
    end
endmodule
