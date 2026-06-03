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

        # cocotb-coverage taken from PyPI release
        cocotb-coverage = pkgs.python313Packages.buildPythonPackage rec {
          pname = "cocotb-coverage";
          version = "2.0";
          format = "wheel";
          src = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/ae/cf/c49f7a475f2d0303007f8a5aaf9e3cbe098179c6bb956d770e881e88735a/cocotb_coverage-${version}-py3-none-any.whl";
            sha256 = "1f65a15f7431b254bcb5f5a5d1b4676c5e89919546de4faaa4dbfda86f8300cb";
          };
          propagatedBuildInputs = with pkgs.python313Packages; [
            cocotbUnbroken
            python-constraint
            pyyaml
          ];
          dontCheckRuntimeDeps = true;
          doCheck = false;
        };

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
          cocotb-coverage
          pytest
          pytest-asyncio
          pyusb
          uuid6
        ]);

        # iCE40 + ECP5 FOSS toolchains
        fpgaTools = with pkgs; [
          yosys
          nextpnr        # provides nextpnr-ice40 and nextpnr-ecp5
          icestorm       # iCE40 bitstream tools (icepack, icemulti, ...)
          trellis        # ECP5 bitstream tools (ecppack, ecppll, ...)
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
            printf "  ecppack   %s\n" "$(ecppack --version 2>&1 | head -1)"
            printf "  iverilog  %s\n" "$(iverilog -V 2>&1 | head -1)"
            printf "  cocotb    %s\n" "$(cocotb-config --version 2>/dev/null)"
            printf "  python    %s\n" "$(python --version)"
            echo "  amaranth  $(python -c 'import amaranth; print(amaranth.__version__)' 2>/dev/null)"
            echo
          '';
        };
      });
}
