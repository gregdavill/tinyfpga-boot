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


class SerialSource(enum.Enum):
    """Where the USB iSerialNumber comes from."""
    #: The flash's 64-bit Read-Unique-ID, rendered as hex.
    FLASH_UID = "flash_uid"
    #: The board `uuid` parsed out of the flash security page (ASCII).
    SECURITY_PAGE = "security_page"


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
    product: str
    board_id: str
    model: str
    url: str
    scsi_vendor: str
    scsi_product: str

    serial_source: SerialSource = SerialSource.FLASH_UID

    # SB_WARMBOOT slot to reload after a complete UF2 transfer.
    reload_slot: int = 1
    # Flash byte offset of the slot's image region. The host's UF2 carries
    # addresses relative to 0; the gateware adds this base so the image
    # lands where SB_WARMBOOT(slot) reconfigures from.
    reload_image_offset: int = 0
    # Sync-domain cycles of quiet before reconfiguring.
    reload_idle_cycles: int | None = None


# Per-board modules, collected into a name -> BoardConfig registry.
from .tinyfpga_bx import board as tinyfpga_bx    # noqa: E402
from .orangecrab import board as orangecrab      # noqa: E402
from .ecpbreaker import board as ecpbreaker      # noqa: E402

BOARDS = {b.name: b for b in (tinyfpga_bx, orangecrab, ecpbreaker)}
