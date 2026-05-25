# cocotb integration test environment

> `python sim/elaborate.py` produces a Verilog output from the
> projects `Top`. bobotb runs against this verilog simulated by icarus

This directory holds an integration-level testbench for the bootloader.
Where the in-tree unittests (`python -m blocks.qspi`) exercise individual
Amaranth modules with `amaranth.sim.Simulator`, this environment exercises
the whole `top.Top` against simulated USB host traffic and a simulated SPI
flash, at the package-pin level.

Two targets:

* **`make`** (default, `TARGET=fs`) — the iCE40 TinyFPGA BX full-speed DUT.
  A wire-level USB host (`usb_host.py`) bit-bangs D+/D- (NRZI + bit-stuffing)
  against the gateware PHY.
* **`make TARGET=hs`** — the ECP5 *ecpbreaker* high-speed DUT. A behavioural
  USB3343 ULPI PHY (`ulpi_phy.py`) carries packets as a byte stream and runs
  the high-speed chirp handshake; the host (`usb_host_hs.py`) reuses the FS
  transaction layer over it. Covers 512-byte bulk packets, HS enumeration,
  the device-qualifier / other-speed descriptors, and SOF handling.

## Layout

```
sim/
├── README.md
├── Makefile                ← cocotb entry point (icarus); TARGET=fs|hs
├── cocotb_platform.py      ← TinyFPGABXPlatform minus the toolchain
├── cocotb_hs_platform.py   ← ECPBreaker (ECP5) platform minus the toolchain
├── elaborate.py            ← Platform.prepare() → verilog.convert_fragment
├── ice40_pll_sim.v         ← behavioural SB_PLL40_CORE (FS)
├── ecp5_cells_sim.v        ← behavioural ECP5 cells: EHXPLLL, DDR, IO buffers (HS)
└── cocotb_tests/
    ├── dut_pins.py             ← name aliases for the auto-generated ports
    ├── usb_packets.py          ← speed-independent PID/CRC/packet builders
    ├── usb_host.py             ← USB FS host (NRZI + bit-stuff + transactions)
    ├── usb_host_hs.py          ← USB HS host (FS transaction layer over ULPI)
    ├── ulpi_phy.py             ← behavioural USB3343 ULPI PHY + HS chirp
    ├── spi_flash_model.py      ← QSPI flash (UID / fast read / page program / erase)
    ├── test_bootloader.py      ← FS cocotb test cases
    └── test_bootloader_hs.py   ← HS cocotb test cases
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

High-speed (ECP5 / ULPI) suite:

```bash
cd sim && make TARGET=hs
```

Single test (regex filter):

```bash
cd sim && make COCOTB_TEST_FILTER=test_enumeration
cd sim && make TARGET=hs COCOTB_TEST_FILTER=test_hs_enumeration
```

The ULPI PHY model logs its protocol steps at DEBUG when `ULPI_DEBUG=1`.
