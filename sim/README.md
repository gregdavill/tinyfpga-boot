# cocotb integration test environment

> `python sim/elaborate.py` produces a Verilog output from the
> projects `Top`. bobotb runs against this verilog simulated by icarus

This directory holds an integration-level testbench for the TinyFPGA BX
bootloader. Where the in-tree unittests (`python -m blocks.qspi`)
exercise individual Amaranth modules with `amaranth.sim.Simulator`,
this environment exercises the whole `top.Top` against simulated USB
host traffic and a simulated SPI flash, at the package-pin level.

## Layout

```
sim/
├── README.md
├── Makefile                ← cocotb entry point (icarus)
├── cocotb_platform.py      ← TinyFPGABXPlatform minus the toolchain
├── elaborate.py            ← Platform.prepare() → verilog.convert_fragment
└── cocotb_tests/
    ├── dut_pins.py             ← name aliases for the auto-generated ports
    ├── usb_host.py             ← USB FS host (NRZI + bit-stuff + transactions)
    ├── spi_flash_model.py      ← QSPI flash (UID / fast read / page program / erase)
    └── test_bootloader.py      ← cocotb test cases
```

## Tests

`cocotb_tests/test_bootloader.py` covers:

- **boot:** wait for `usb_pullup`, confirm the first flash
  transaction the model saw was `0x4B` Read Unique ID
- **descriptor:** GET_DESCRIPTOR(device) returns the right VID/PID
- **enumeration:** SET_ADDRESS, full device + config descriptor walk,
  SET_CONFIGURATION
- **string-serial:** iSerialNumber string descriptor contains the
  UID bytes the flash model planted
- **uf2-write:** a UF2 block at EP1 OUT shows up as
  WREN → sector-erase → page-program at the right offset

## Running

```bash
nix develop
cd sim && make
```

Single test:

```bash
cd sim && make TESTCASE=test_enumeration
```
