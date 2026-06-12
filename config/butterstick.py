"""ButterStick r1.0 (ECP5 LFE5UM5G-85F)"""

from amaranth import *
from amaranth.build import *
from amaranth.vendor import LatticeECP5Platform
from amaranth_boards.resources import *

from platforms import ECP5Mixin
from config import BoardConfig, SerialSource, ECP5_SLOT1_OFFSET
from staysource import ButtonStaySource, WriteEnableStaySource


class _VccIoEnable(Elaboratable):
    """Bring up ButterStick's three adjustable VccIo rails.

    Drive each rail to an assumed safe ~1.8 V. ULPI is driven by VIO2

    `power_good` then asserts once the enabled rails have had time to rise
    (POWER_GOOD_MS)
    """

    # 16-bit PDM level -> ~1.8 V.
    LEVEL         = 45000
    SETTLE_BITS   = 11      # ~2048 sync cycles before enabling the regulators
    SYNC_HZ       = 60_000_000   # ULPI HS sync domain
    POWER_GOOD_MS = 10      # rail rise time allowed before USB may enumerate

    def __init__(self):
        # High once the rails are enabled and given POWER_GOOD_MS to settle.
        self.power_good = Signal()

    def elaborate(self, platform):
        m = Module()
        vccio = platform.request("vccio_ctrl", dir={"pdm": "o", "en": "o"})

        # One first-order PDM per rail. `out` (the MSB carry) feeds back into the
        # accumulator, so the average duty tracks LEVEL/2**16.
        for i in range(len(vccio.pdm.o)):
            sigma = Signal(18)
            out   = Signal()
            m.d.comb += out.eq(sigma[17])
            m.d.sync += sigma.eq(sigma + Cat(Const(self.LEVEL, 16), out, out))
            m.d.comb += vccio.pdm.o[i].eq(out)

        # Let the PDM-derived references settle, then latch the rails on.
        settle = Signal(self.SETTLE_BITS)
        with m.If(~settle.all()):
            m.d.sync += settle.eq(settle + 1)
        en = Signal()
        m.d.comb += en.eq(settle.all())
        m.d.comb += vccio.en.o.eq(en)

        # Hold power_good low until the rails have had POWER_GOOD_MS to rise.
        good_max = int(self.POWER_GOOD_MS * 1e-3 * self.SYNC_HZ)
        good_cnt = Signal(range(good_max + 1))
        with m.If(en & (good_cnt != good_max)):
            m.d.sync += good_cnt.eq(good_cnt + 1)
        m.d.comb += self.power_good.eq(good_cnt == good_max)

        return m


class ButterStickR1_0Platform(ECP5Mixin, LatticeECP5Platform):
    """ButterStick r1.0 (ECP5 LFE5UM5G-85F), ULPI high-speed USB."""

    device      = "LFE5UM5G-85F"
    package     = "BG381"
    speed       = "8"
    default_clk = "clk30"

    fpga_family = "ecp5"
    usb_phy     = "ulpi_hs"
    flash_clk   = "usrmclk"

    default_usb_connection = "ulpi"

    resources = [
        Resource("clk30", 0, Pins("B12", dir="i"), Clock(30e6),
                 Attrs(IO_TYPE="LVCMOS33")),

        # PROGRAMN on FPGA ball R3: pulling it low (open-drain) reloads the FPGA.
        Resource("reconfigure", 0, PinsN("R3", dir="o"),
                 Attrs(IO_TYPE="LVCMOS33", OPENDRAIN="ON")),

        # User button (active-low, DDR3 bank -> SSTL135 referenced input).
        Resource("button", 0, PinsN("U16", dir="i"), Attrs(IO_TYPE="SSTL135_I")),

        # 7 x RGB status LEDs: `a` selects the LED, `c` the colour channel.
        Resource("led_rgb_multiplex", 0,
            Subsignal("a", Pins("C13 D12 U2 T3 D13 E13 C16", dir="o")),
            Subsignal("c", Pins("T1 U1 R1", dir="o")),
            Attrs(IO_TYPE="LVCMOS33"),
        ),

        # Config flash: cs + dq on the real balls; clk omitted (driven via USRMCLK).
        Resource("spi_flash_4x", 0,
            Subsignal("cs", PinsN("R2", dir="o")),
            Subsignal("dq", Pins("W2 V2 Y2 W1", dir="io")),
            Attrs(IO_TYPE="LVCMOS33"),
        ),

        # Adjustable VccIo control
        Resource("vccio_ctrl", 0,
            Subsignal("pdm", Pins("V1 E11 T2", dir="o")),
            Subsignal("en",  Pins("E12", dir="o")),
            Attrs(IO_TYPE="LVCMOS33"),
        ),

        # USB3343 ULPI high-speed PHY
        ULPIResource("ulpi", 0,
            data="B9 C6 A7 E9 A8 D9 C10 C7",
            clk="B6", clk_dir="o", dir="A6", nxt="B8",
            stp="C8", rst="C9", rst_invert=True,
            attrs=Attrs(IO_TYPE="LVCMOS18", SLEWRATE="FAST")),
    ]

    connectors = []

    def create_clocks(self, m):
        # ECP5Mixin sets up the PLL + 60 MHz sync (ULPI HS path).
        # Bring adjustable VccIo rails up so the 1.8 V ULPI bank is powered.
        super().create_clocks(m)
        m.submodules.vccio = vccio = _VccIoEnable()
        # Top gates USB enumeration on this (held off until the rails settle).
        self._vccio_power_good = vccio.power_good

    def usb_connect_ok(self):
        return self._vccio_power_good

    def create_status_led(self, m, status):
        """Status indicator on LED0 of the multiplexed RGB bar."""
        from blocks.status_led import MultiplexRgbStatusLed

        led = self.request("led_rgb_multiplex", 0, dir={"a": "o", "c": "o"})
        m.submodules.status_led = ind = MultiplexRgbStatusLed(n=7, clk_freq=60_000_000)
        m.d.comb += [
            ind.status.eq(status),
            led.a.o.eq(ind.sel),
            led.c.o.eq(ind.rgb),
        ]


board = BoardConfig(
    name="butterstick",
    platform=ButterStickR1_0Platform,
    vid=0x1209,
    pid=0x5af1,
    manufacturer="GsD",
    board_id="ButterStick-r1.0",
    model="ButterStick r1.0",
    url="https://github.com/butterstick-fpga",
    scsi_vendor="BUTTER",
    scsi_product="UF2 Bootloader",
    serial_source=SerialSource.SECURITY_PAGE,
    # W25Q128-class security registers at 0x1000/0x2000/0x3000; 1 << (8 + 4).
    security_page_addr_offset_bits=4,
    reload_slot=1,
    reload_image_offset=ECP5_SLOT1_OFFSET,
    stay_sources=(ButtonStaySource, WriteEnableStaySource),
    ecppack_opts="--compress --freq 38.8",
)
