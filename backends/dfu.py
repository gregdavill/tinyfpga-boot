"""DFU backend — dload over EP0 control transfers.

Presents a single USB DFU 1.1 (mode)

DFU uses EP0, so there are no data endpoints.
"""

import struct

from amaranth import Signal, ResetInserter
from amaranth.lib import wiring

from blocks.flash import QspiFlash
from blocks.dfu import DFUHandler, DFUState
from blocks.dual_bank_writer import DualBankWriter
from tools.ecp_bitstream import flash_header

from . import Backend, Reconfig, Status


# wTransferSize advertised in the DFU functional descriptor. The DFUHandler's
# download FIFO is 512 bytes and its status phase drains once 256 bytes are
# free, so keep chunks at 256.
_TRANSFER_SIZE = 256


def _dfu_functional_descriptor(*, transfer_size, detach_timeout_ms=1000):
    """USB DFU 1.1 functional descriptor (9 bytes, bDescriptorType 0x21).

    bmAttributes 0x05 = bitCanDnload | bitManifestationTolerant: accept
    downloads and reboot ourselves rather than requiring a USB reset. Upload
    and bitWillDetach are not supported (we are always in DFU mode).
    """
    return struct.pack(
        "<BBBHHH",
        9,            # bFunctionLength
        0x21,         # bDescriptorType = DFU FUNCTIONAL
        0x05,         # bmAttributes
        detach_timeout_ms,
        transfer_size,
        0x0110,       # bcdDFUVersion = 1.1
    )


class DfuBackend(Backend):
    device_class = (0, 0, 0)
    personality = "DFU"

    _RAM_ALT = 1   # bAlternateSetting that targets the QSPI PSRAM

    def __init__(self, config, *, hs, alloc=None):
        super().__init__(config, hs=hs, alloc=alloc)

        self.usb_ids = (config.vid, config.pid)
        self._IF_NUM = self.alloc.interface()

        self.has_ram_bank = config.has_ram_bank
        if self.has_ram_bank:
            self.areas     = [config.reload_image_offset or 0, 0]
            self.alt_names = [f"{config.model} (FLASH)", f"{config.model} (RAM)"]
        else:
            self.areas     = [config.reload_image_offset or 0]
            self.alt_names = [config.model]

        self.dfu = DFUHandler(if_num=self._IF_NUM, areas=self.areas)
        if self.has_ram_bank:
            self.writer = DualBankWriter(
                header_bytes=flash_header(),
                base_addr=config.reload_image_offset)
            self.cfg_ctrl_o = self.writer.cfg_ctrl_o
        else:
            self.flash = QspiFlash()

    def populate_configuration(self, c, *, bulk_mps):
        c.bMaxPower = 100

        # One InterfaceDescriptor per alternate setting
        # `dfu-util -a N` selects it
        for alt, _base in enumerate(self.areas):
            with c.InterfaceDescriptor() as i:
                i.bInterfaceNumber   = self._IF_NUM
                i.bAlternateSetting  = alt
                i.bInterfaceClass    = 0xFE  # Application Specific
                i.bInterfaceSubclass = 0x01  # DFU
                i.bInterfaceProtocol = 0x02  # DFU mode
                # dfu-util surfaces the alt-setting string as its `name=` field
                i.iInterface = self.alt_names[alt]
                i.add_subordinate_descriptor(
                    _dfu_functional_descriptor(transfer_size=_TRANSFER_SIZE))

    def request_handlers(self):
        return [self.dfu]

    def msft_compat_ids(self):
        # DFU is an Application-Specific interface with no in-box Windows
        # driver; bind WinUSB so dfu-util works
        return [(self._IF_NUM, "WINUSB")]

    def endpoints(self):
        return []  # EP0 control only

    def build(self, m, *, usb):
        # DFUHandler is added to the control endpoint in Top. Manifestation
        # (zero-length final DNLOAD) latches a flush/reload request.
        flushing = Signal()
        with m.If(self.dfu.manifest):
            m.d.sync += flushing.eq(1)

        if not self.has_ram_bank:
            m.submodules.flash = flash = ResetInserter(usb.reset_detected)(self.flash)
            # DFUHandler emits an (addr, data) stream straight into the flash writer.
            wiring.connect(m, self.dfu.source, flash.i)
            m.d.comb += flash.done.eq(flushing)
            self.qo, self.qi = flash.qo, flash.qi
            arm = flushing
            activity = self.dfu.source.valid | flash.qo.valid
        else:
            # The shared dual-bank writer routes the download to FLASH or PSRAM
            # by alt-setting.
            m.submodules.writer = writer = self.writer
            m.d.comb += [
                writer.i.p.eq(self.dfu.source.p),
                writer.i.valid.eq(self.dfu.source.valid),
                self.dfu.source.ready.eq(writer.i.ready),
                writer.ram_select.eq(self.dfu.area_sel == self._RAM_ALT),
                writer.done.eq(flushing),
                writer.clear.eq(usb.reset_detected),
            ]
            self.qo, self.qi = writer.qo, writer.qi
            arm = writer.arm
            activity = self.dfu.source.valid | writer.active

        # Status: DONE (manifestation / boot-arm) > ACTIVE (writing) > IDLE.
        # TODO: report ERROR
        #   with m.If(self.dfu.state == DFUState.dfuERROR.value):
        #       m.d.comb += self.status.eq(Status.ERROR)
        with m.If(arm):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(activity):
            m.d.comb += self.status.eq(Status.ACTIVE)

        # Hold off the post-download warmboot until the host has stopped
        # talking. After the download dfu-util keeps polling GETSTATUS.
        return Reconfig(arm=arm, activity=activity | usb.tx_activity_led)
