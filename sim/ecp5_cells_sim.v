// Self-contained behavioural ECP5 primitives for cocotb simulation.
//
// The HS (ecpbreaker) DUT is emitted straight from Amaranth's rtlil->verilog
// backend with no synthesis, so the only hard cells that appear are the IO
// buffers Amaranth instantiates plus the EHXPLLL the platform's PLL wrapper
// adds. yosys' own `ecp5/cells_sim.v` can't be fed to iverilog directly (its
// `include`s are unguarded and the DDR/PLL cells are blackboxes anyway), so
// we model exactly the cells this DUT uses:
//
//     BB IB                 - bidirectional / input IO buffers
//     OFS1P3DX              - output flip-flop with async clear + clock enable
//     ODDRX1F IDDRX1F       - DDR output / input registers (QSPI flash clock+IO)
//     EHXPLLL               - PLL (passthrough; cocotb drives CLKI post-PLL)
//
// Keep this list in sync with the primitives the emitted `sim_top_hs.v`
// instantiates (grep for `\b(BB|IB|...)\b`).

`timescale 1ns / 1ps

// --- IO buffers ------------------------------------------------------------

// Bidirectional buffer. B is the external (top-level) pad; I is the fabric
// drive, T the tristate enable (1 = hi-Z / input), O the fabric read.
module BB(input I, input T, output O, inout B);
    assign B = (T === 1'b1) ? 1'bz : I;
    assign O = B;
endmodule

// Input buffer. I is the external pad; O goes to the fabric.
module IB(input I, output O);
    assign O = I;
endmodule

// Output buffer (declared for completeness / robustness to pin-map changes).
module OB(input I, output O);
    assign O = I;
endmodule

// Tristate output buffer (used for the open-drain `reconfigure` line and the
// FPGA-driven ULPI clock). O is the external pad; T=1 -> hi-Z.
module OBZ(input I, input T, output O);
    assign O = (T === 1'b1) ? 1'bz : I;
endmodule

// --- Output flip-flop ------------------------------------------------------

// OFS1P3DX: D flip-flop with active-high async clear (CD) and clock enable
// (SP), clocked on SCLK. Matches the TRELLIS_FF the yosys techmap targets.
module OFS1P3DX(input CD, input D, input SP, input SCLK, output reg Q);
    parameter GSR = "ENABLED";
    always @(posedge SCLK or posedge CD)
        if (CD)      Q <= 1'b0;
        else if (SP) Q <= D;
endmodule

// --- DDR registers ---------------------------------------------------------
//
// These reproduce the exact gearing/latency of Amaranth's ECP5 `io.DDRBuffer`
// as captured by `blocks/iostream.py:SimulatableDDRBuffer` - the model the
// QSPI controller's `IOStreamer` (ratio=2, offset=0) is built and tested
// against.

// ODDRX1F / IDDRX1F: silicon-accurate gearing/latency of the ECP5 DDR primitives
module ODDRX1F(output reg Q, input wire SCLK, input wire D0, input wire D1, input wire RST);
    parameter GSR = "ENABLED";
    reg D1_f = 0, D1_ff = 0, D1_fff = 0;
    reg D0_f = 0, D0_ff = 0;
    always @(posedge SCLK) begin
        Q      <= D0_ff;
        D0_f   <= D0;
        D0_ff  <= D0_f;
        D1_f   <= D1;
        D1_ff  <= D1_f;
        D1_fff <= D1_ff;
    end
    always @(negedge SCLK)
        Q <= D1_fff;
endmodule

module IDDRX1F(output reg Q0, output reg Q1, input wire SCLK, input wire D, input wire RST);
    parameter GSR = "ENABLED";
    reg Q_neg_f = 0, Q_neg_ff = 0, Q_pos_f = 0;
    always @(posedge SCLK) begin
        Q_pos_f <= D;
        Q1      <= Q_pos_f;
        Q0      <= Q_neg_ff;
    end
    always @(negedge SCLK) begin
        Q_neg_f  <= D;
        Q_neg_ff <= Q_neg_f;
    end
endmodule

// --- PLL -------------------------------------------------------------------

// Behavioural EHXPLLL: pass the reference straight through to every output and
// assert LOCK after ~1 us. cocotb drives CLKI at the *post-PLL* frequency
// (60 MHz for the HS sync domain). The ~1 us lock delay leaves the platform's
// `sync.rst = ~locked` reset stretch observable.
module EHXPLLL (
    input        CLKI,
    input        CLKFB,
    input        PHASESEL1,
    input        PHASESEL0,
    input        PHASEDIR,
    input        PHASESTEP,
    input        PHASELOADREG,
    input        STDBY,
    input        PLLWAKESYNC,
    input        RST,
    input        ENCLKOP,
    input        ENCLKOS,
    input        ENCLKOS2,
    input        ENCLKOS3,
    output       CLKOP,
    output       CLKOS,
    output       CLKOS2,
    output       CLKOS3,
    output reg   LOCK,
    output       INTLOCK,
    output       REFCLK,
    output       CLKINTFB
);
    parameter FREQUENCY_PIN_CLKI = "60.0";
    parameter ICP_CURRENT        = "6";
    parameter LPF_RESISTOR       = "16";
    parameter MFG_ENABLE_FILTEROPAMP = "1";
    parameter MFG_GMCREF_SEL     = "2";
    parameter FEEDBK_PATH        = "INT_OS3";
    parameter CLKOS3_ENABLE      = "ENABLED";
    parameter CLKOS3_DIV         = 1;
    parameter CLKFB_DIV          = 1;
    parameter CLKI_DIV           = 1;
    parameter CLKOP_ENABLE       = "ENABLED";
    parameter CLKOP_DIV          = 1;
    parameter CLKOP_FPHASE       = 0;
    parameter CLKOP_CPHASE       = 0;
    parameter CLKOS_ENABLE       = "DISABLED";
    parameter CLKOS_DIV          = 1;
    parameter CLKOS_FPHASE       = 0;
    parameter CLKOS_CPHASE       = 0;
    parameter CLKOS2_ENABLE      = "DISABLED";
    parameter CLKOS2_DIV         = 1;
    parameter CLKOS2_FPHASE      = 0;
    parameter CLKOS2_CPHASE      = 0;

    assign CLKOP    = CLKI;
    assign CLKOS    = CLKI;
    assign CLKOS2   = CLKI;
    assign CLKOS3   = CLKI;
    assign REFCLK   = CLKI;
    assign CLKINTFB = CLKI;
    assign INTLOCK  = LOCK;

    initial begin
        LOCK = 1'b0;
        #1000 LOCK = 1'b1;
    end
endmodule
