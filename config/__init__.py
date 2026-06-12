"""Board configuration.

`BoardConfig` is the functional/identity description of a board (USB IDs,
strings, serial source, reload layout). Platform-physical traits (FPGA
family, USB PHY, flash-clock routing, clocking) live on the platform
class instead — see `platforms.py`.
"""

import enum
from dataclasses import dataclass


# --- Multiboot layout ---------------------------------------------------
#
# SB_WARMBOOT selects a slot (S1:S0); the flash offset for each slot lives
# in the icemulti multiboot header at flash address 0. icemulti aligns
# images to 2^ALIGN_BITS (`-A<n>`); an iCE40 LP8K bitstream needs 256 KiB
# slots.
MULTIBOOT_ALIGN_BITS = 18                        # 256 KiB slots
SLOT1_OFFSET         = 1 << MULTIBOOT_ALIGN_BITS  # 0x40000

# ECP5 use `--bootaddr` points at slot 1 directly. An ECP5 bitstream can be MiB-scale.
# Give each slot 2 MiB.
ECP5_SLOT1_OFFSET = 0x200000  # 2 MiB


class SerialSource(enum.Enum):
    """Where the USB iSerialNumber comes from."""
    #: The flash's 64-bit Read-Unique-ID, rendered as hex.
    FLASH_UID = "flash_uid"
    #: The board `uuid` parsed out of the flash security page (ASCII).
    SECURITY_PAGE = "security_page"


class Backend(enum.Enum):
    """The USB personality the bootloader presents. Exactly one is active
    per build; they never coexist at runtime."""
    #: Mass-Storage / UF2 drag-and-drop
    UF2_MSC = "uf2"
    #: TinyFPGA CDC-ACM serial USB->SPI bridge
    TINYFPGA_SERIAL = "serial"
    #: USB DFU 1.1 download over EP0
    DFU = "dfu"


@dataclass
class BoardConfig:
    #: Board identifier (the `build.py --board` key).
    name: str
    #: The amaranth Platform subclass for this board (a class, not an
    #: instance). Carries the platform-physical traits Top reads.
    platform: type

    vid: int
    pid: int
    manufacturer: str
    board_id: str
    model: str
    url: str
    scsi_vendor: str
    scsi_product: str

    serial_source: SerialSource = SerialSource.FLASH_UID
    backend: Backend = Backend.UF2_MSC

    # SECURITY_PAGE read address is flash-family specific: the SecurityPage
    # reader uses addr = 1 << (8 + this).
    security_page_addr_offset_bits: int = 0

    # SB_WARMBOOT slot to reload after a complete UF2 transfer.
    reload_slot: int = 1
    # Flash byte offset of the slot's image region. The host's UF2 carries
    # addresses relative to 0; the gateware adds this base so the image
    # lands where SB_WARMBOOT(slot) reconfigures from.
    reload_image_offset: int = 0
    # Sync-domain cycles of quiet before reconfiguring.
    reload_idle_cycles: int | None = None

    # Extra ecppack flags
    ecppack_opts: str = "--compress --spimode qspi --freq 38.8"

    # A non-empty tuple enables auto-boot: at power-on the bootloader reboots
    # into the slot-1 app unless one of these sources says stay.
    # Empty (default): always enumerate, never auto-boot.
    stay_sources: tuple = ()

    # Dual-bank (FLASH / QSPI PSRAM) support.
    has_ram_bank: bool = False
    ram_family_id: int = 0


# Per-board modules, collected into a name -> BoardConfig registry.
from .tinyfpga_bx import board as tinyfpga_bx    # noqa: E402
from .orangecrab import board as orangecrab      # noqa: E402
from .ecpbreaker import board as ecpbreaker      # noqa: E402

BOARDS = {b.name: b for b in (tinyfpga_bx, orangecrab, ecpbreaker)}
