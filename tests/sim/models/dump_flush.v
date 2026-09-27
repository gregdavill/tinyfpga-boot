// Periodic $dumpflush so data lands on disk across if cocotb encounters TimeoutFailure
module dump_flush();
    initial begin
        $dumpfile("build/sim_icarus/sim_top.vcd");
        $dumpvars(0, sim_top);
        forever begin
            #100000;
            $dumpflush;
        end
    end
endmodule
