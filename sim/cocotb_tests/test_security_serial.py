"""cocotb test for the security-page ASCII UUID serials.

This module is run against the DUT elaborated with
``serial_source="security_page"``
"""

import cocotb

from .usb_host import USBHost
from .spi_flash_model import SPIFlashModel


DESC_DEVICE = 0x01
DESC_STRING = 0x03

# Must match the `uuid` in SPIFlashModel's default `security_data`.
EXPECTED_UUID = "CAFEBABE-DEAD-BEEF-0123-456789ABCDEF"


@cocotb.test(timeout_time=3_000, timeout_unit="us")
async def test_security_page_serial_is_uuid(dut):
    """The device parses boardmeta.uuid from the security page at boot
    and reports it verbatim as the iSerialNumber string descriptor."""
    flash = SPIFlashModel(dut)          # default security_data carries EXPECTED_UUID
    cocotb.start_soon(flash.run())

    host = USBHost(dut)
    await host.start()                  # blocks until pullup, i.e. boot read done
    await host.reset_bus()

    # The Read Security Registers (0x48) command must have run at boot,
    # before USB enumeration — and there must be no stray 0x4B UID read
    assert any(t.opcode == 0x48 for t in flash.transactions), \
        "device never issued Read Security Registers (0x48)"
    assert not any(t.opcode == 0x4B for t in flash.transactions), \
        "unexpected Read-UID (0x4B) in security_page configuration"

    await host.set_address(0x12)
    dev = await host.get_descriptor(descriptor_type=DESC_DEVICE, length=18)
    i_serial = dev[16]
    assert i_serial != 0, "device declared iSerialNumber = 0"

    # Request the full descriptor (the 36-char UUID is a 74-byte
    # descriptor, larger than the 64 a single small read would cap at).
    str_desc = await host.get_descriptor(
        descriptor_type=DESC_STRING, index=i_serial,
        lang_id=0x0409, length=255,
    )
    assert str_desc[1] == DESC_STRING, f"not a string descriptor: {str_desc!r}"
    text = str_desc[2:str_desc[0]].decode("utf-16-le")
    assert text == EXPECTED_UUID, f"serial {text!r} != {EXPECTED_UUID!r}"
