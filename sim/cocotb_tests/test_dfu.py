"""cocotb test cases for the DFU backend.

Drives the bootloader's DFU backend over EP0 control transfers: walk the
descriptors, GET_STATUS, DNLOAD a block, and the zero-length manifestation
that flushes the page and arms SB_WARMBOOT.
"""

import cocotb
from cocotb.triggers import Timer

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


# Descriptor type constants (USB 2.0 §9.4)
DESC_CONFIGURATION = 0x02

# DFU class (USB DFU 1.1)
DFU_FUNCTIONAL_DESC = 0x21
DFU_IF_CLASS, DFU_IF_SUBCLASS, DFU_IF_PROTO = 0xFE, 0x01, 0x02
DFUSTATE_dfuIDLE = 2

# DFU class requests
DFU_DNLOAD    = 0x01
DFU_GETSTATUS = 0x03
# bmRequestType: class | interface recipient
RT_CLASS_IF_OUT = 0x21   # host -> device
RT_CLASS_IF_IN  = 0xA1   # device -> host

# Standard SET_INTERFACE (alt-setting select): standard | interface recipient, OUT
RT_STD_IF_OUT = 0x01
SET_INTERFACE = 0x0B

# Single DFU area maps to the slot-1 reload region (see _fs_sim_config).
DFU_AREA_BASE = 0x40000

# Microsoft OS 1.0 descriptors — how Windows auto-installs WinUSB on the
# driverless DFU interface (see top.py). The 0xEE OS string advertises a
# vendor request code; here it must match MSFT_VENDOR_CODE in top.py.
MS_OS_STRING_INDEX  = 0xEE
MSFT_VENDOR_CODE    = 0xEE
MS_COMPAT_ID_INDEX  = 0x0004    # wIndex for the Extended Compat ID descriptor
RT_VENDOR_DEV_IN    = 0xC0      # vendor | device recipient, device -> host

TIMEOUT_ENUM = 2_000   # full enumeration walk
TIMEOUT_DFU  = 5_000   # DNLOAD + page-program drain
TIMEOUT_BOOT = 8_000   # DNLOAD + manifest + ~2.5 ms idle window


async def _bringup(dut):
    """Common per-test bring-up. Returns (host, flash)."""
    flash = SPIFlashModel(dut)
    cocotb.start_soon(flash.run())
    host = USBHost(dut)
    await host.start()      # also waits for usb_pullup
    await host.reset_bus()
    return host, flash


def _walk_descriptors(cfg):
    """Yield (bDescriptorType, bytes) for each descriptor in a config blob."""
    off = 0
    while off + 2 <= len(cfg):
        length = cfg[off]
        if length == 0:
            break
        yield cfg[off + 1], cfg[off:off + length]
        off += length


async def _enumerate(host):
    """Address the device and read its full configuration descriptor."""
    await host.get_descriptor(descriptor_type=0x01, length=18)  # latch bMaxPacketSize0
    await host.set_address(0x12)
    head = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=9)
    total = head[2] | (head[3] << 8)
    cfg = await host.get_descriptor(descriptor_type=DESC_CONFIGURATION, length=total)
    await host.set_configuration(1)
    return cfg

@cocotb.test(timeout_time=TIMEOUT_BOOT, timeout_unit="us")
async def test_dfu_warmboots_after_host_goes_idle(dut):
    """The post-download warmboot waits until the host stops talking, so the
    device survives dfu-util's GETSTATUS polling."""
    host, _ = await _bringup(dut)
    await _enumerate(host)

    payload = bytes((i ^ 0x5A) & 0xFF for i in range(64))
    await host.control_out(RT_CLASS_IF_OUT, DFU_DNLOAD, w_value=0, w_index=0, data=payload)
    await host.control_out(RT_CLASS_IF_OUT, DFU_DNLOAD, w_value=1, w_index=0, data=b"")

    # While the host keeps polling GETSTATUS the device must stay up: each poll
    # is USB tx activity that holds off the warmboot. Without that gate the idle
    # window (~85 µs in sim) would elapse during this loop and reboot early.
    for _ in range(40):
        await host.control_in(RT_CLASS_IF_IN, DFU_GETSTATUS, w_index=0, w_length=6)
        try:
            assert int(dut.reconfigure.boot.value) == 0, \
                "SB_WARMBOOT.BOOT fired while the host was still polling GETSTATUS"
        except ValueError:
            continue

    # Host goes quiet -> warmboot fires after the idle window.
    boot_seen = False
    for _ in range(3000):
        await Timer(1, unit="us")
        try:
            if int(dut.reconfigure.boot.value) == 1:
                boot_seen = True
                break
        except (AttributeError, ValueError):
            continue
    assert boot_seen, "SB_WARMBOOT.BOOT never pulsed after the host went idle"


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_dfu_enumeration(dut):
    """The config descriptor advertises a DFU-mode interface plus a DFU
    functional descriptor with the expected transfer size."""
    host, _ = await _bringup(dut)
    cfg = await _enumerate(host)

    descs = list(_walk_descriptors(cfg))

    interfaces = [d for t, d in descs if t == 0x04]
    assert interfaces, "no interface descriptor found"
    iface = interfaces[0]
    assert (iface[5], iface[6], iface[7]) == (DFU_IF_CLASS, DFU_IF_SUBCLASS, DFU_IF_PROTO), \
        f"interface class triple = {iface[5:8].hex()}, expected FE/01/02"

    functional = [d for t, d in descs if t == DFU_FUNCTIONAL_DESC]
    assert functional, "no DFU functional descriptor (0x21) found"
    func = functional[0]
    assert len(func) == 9, f"DFU functional descriptor is {len(func)} bytes, expected 9"
    transfer_size = func[5] | (func[6] << 8)
    assert transfer_size == 256, f"wTransferSize = {transfer_size}, expected 256"


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_dfu_winusb_auto_install(dut):
    """Windows' MS OS 1.0 flow: read the 0xEE OS string for the vendor code,
    then fetch the Extended Compat ID descriptor and find "WINUSB" bound to the
    DFU interface. This is what lets Windows load winusb.sys without Zadig."""
    host, _ = await _bringup(dut)
    await _enumerate(host)

    # 1) OS string descriptor at index 0xEE: "MSFT100" + vendor request code.
    os_str = await host.get_descriptor(
        descriptor_type=0x03, index=MS_OS_STRING_INDEX, length=0x12)
    assert os_str[2:16] == "MSFT100".encode("utf-16-le"), \
        f"bad MS OS signature: {os_str.hex()}"
    vendor_code = os_str[16]
    assert vendor_code == MSFT_VENDOR_CODE, \
        f"vendor code {vendor_code:#x} != {MSFT_VENDOR_CODE:#x}"

    # 2) Extended Compat ID descriptor via that vendor request (wIndex = 4).
    compat = await host.control_in(
        RT_VENDOR_DEV_IN, vendor_code,
        w_value=0, w_index=MS_COMPAT_ID_INDEX, w_length=0x28)
    # Header is 16 bytes; each function section is 24. The first section's
    # bFirstInterfaceNumber is the DFU interface and its compatibleID "WINUSB".
    assert compat[16] == 0, f"compat-ID first-interface = {compat[16]}, expected 0"
    assert compat[18:24] == b"WINUSB", \
        f"compatibleID = {compat[18:26].hex()}, expected WINUSB"


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_dfu_get_status_idle(dut):
    """GET_STATUS reports dfuIDLE before any download."""
    host, _ = await _bringup(dut)
    await _enumerate(host)

    status = await host.control_in(RT_CLASS_IF_IN, DFU_GETSTATUS, w_index=0, w_length=6)
    assert len(status) == 6, f"short GET_STATUS response: {status.hex()}"
    assert status[4] == DFUSTATE_dfuIDLE, \
        f"bState = {status[4]}, expected dfuIDLE ({DFUSTATE_dfuIDLE})"


@cocotb.test(timeout_time=TIMEOUT_ENUM, timeout_unit="us")
async def test_dfu_set_interface_acks(dut):
    """SET_INTERFACE (alt-setting select) must ACK, not stall."""
    host, _ = await _bringup(dut)
    await _enumerate(host)

    await host.control_out(RT_STD_IF_OUT, SET_INTERFACE, w_value=0, w_index=0)


@cocotb.test(timeout_time=TIMEOUT_DFU, timeout_unit="us")
async def test_dfu_download_programs_flash(dut):
    """A DNLOAD block followed by the zero-length manifestation lands the
    bytes in flash via WREN -> block erase -> WREN -> page program."""
    host, flash = await _bringup(dut)
    await _enumerate(host)

    # Boot-time UID read etc. is noise; isolate the DFU-driven activity.
    flash.transactions.clear()

    # One full EP0 packet (control_out does not chunk, so keep it <= 64).
    payload = bytes(range(64))
    await host.control_out(RT_CLASS_IF_OUT, DFU_DNLOAD, w_value=0, w_index=0, data=payload)
    # Zero-length DNLOAD = manifestation: flushes the final page.
    await host.control_out(RT_CLASS_IF_OUT, DFU_DNLOAD, w_value=1, w_index=0, data=b"")

    # At divisor=4 the closing page-program + status poll takes a while.
    await Timer(1500, unit="us")

    opcodes = [t.opcode for t in flash.transactions]
    assert 0x06 in opcodes, f"no write-enable observed (opcodes={[hex(o) for o in opcodes]})"
    assert 0xD8 in opcodes, f"no block erase observed (opcodes={[hex(o) for o in opcodes]})"
    program_ops = [t for t in flash.transactions if t.opcode in (0x02, 0x32)]
    assert program_ops, f"no page program observed (opcodes={[hex(o) for o in opcodes]})"
    assert program_ops[0].address == DFU_AREA_BASE, \
        f"programmed at {program_ops[0].address:#x}, expected {DFU_AREA_BASE:#x}"

    assert bytes(flash.memory[DFU_AREA_BASE:DFU_AREA_BASE + len(payload)]) == payload, \
        "flash contents do not match the DFU payload"
