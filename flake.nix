{
  description = "TinyFPGA BX bootloader — Amaranth + cocotb sim";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # nixpkgs marks cocotb 2.0.1 as `broken` on darwin because its
        # bundled test suite doesn't pass; 
        cocotbUnbroken = pkgs.python313Packages.cocotb.overridePythonAttrs (old: {
          meta = (old.meta or {}) // { broken = false; };
          doCheck = false;
          nativeCheckInputs = [];
        });

        # Python interpreter with everything the project imports 
        python = pkgs.python313.withPackages (ps: with ps; [
          # gateware
          amaranth
          amaranth-boards
          luna-usb
          usb-protocol
          # sim + tests
          cocotbUnbroken
          cocotb-bus
          pytest
          pytest-asyncio
        ]);

        # iCE40 FOSS toolchains
        fpgaTools = with pkgs; [
          yosys
          nextpnr
          icestorm
          dfu-util
        ];

        # Simulators for cocotb
        simTools = with pkgs; [
          iverilog
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [ python ] ++ fpgaTools ++ simTools ++ (with pkgs; [
            git
            gnumake
          ]);

          shellHook = ''
            echo
            echo "tinybx-bootloader nix dev shell"
            printf "  yosys     %s\n" "$(yosys -V 2>/dev/null | head -1)"
            printf "  nextpnr   %s\n" "$(nextpnr-ice40 --version 2>&1 | head -1)"
            printf "  iverilog  %s\n" "$(iverilog -V 2>&1 | head -1)"
            printf "  cocotb    %s\n" "$(cocotb-config --version 2>/dev/null)"
            printf "  python    %s\n" "$(python --version)"
            echo "  amaranth  $(python -c 'import amaranth; print(amaranth.__version__)' 2>/dev/null)"
            echo
          '';
        };
      });
}
