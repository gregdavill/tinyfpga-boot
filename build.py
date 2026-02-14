from amaranth_boards.tinyfpga_bx import TinyFPGABXPlatform
from top import Top


if __name__ == "__main__":
    TinyFPGABXPlatform().build(Top(), do_program=False)
