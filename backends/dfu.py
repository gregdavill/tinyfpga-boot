"""DFU backend — dload over EP0 control transfers.

Presents a single USB DFU 1.1 (mode)

DFU uses EP0, so there are no data endpoints.
"""

import struct

from amaranth import Signal, ResetInserter
from amaranth.lib import wiring

from blocks.flash import QspiFlash
from blocks.dfu import DFUHandler, DFUState

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

    _IF_NUM = 0

    def __init__(self, config, *, hs):
        super().__init__(config, hs=hs)

        self.usb_ids = (config.vid, config.pid)

        # One alternate setting -> the reload image region.
        self.areas = [config.reload_image_offset or 0]

        self.dfu   = DFUHandler(if_num=self._IF_NUM, areas=self.areas)
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
                # dfu-util surfaces the alt-setting string as its `name=`
                # field; use the board name so it's identifiable in a listing.
                i.iInterface = self.config.model
                i.add_subordinate_descriptor(
                    _dfu_functional_descriptor(transfer_size=_TRANSFER_SIZE))

    def request_handlers(self):
        return [self.dfu]

    def endpoints(self):
        return []  # EP0 control only

    def build(self, m, *, usb):
        # DFUHandler is added to the control endpoint in Top
        m.submodules.flash = flash = ResetInserter(usb.reset_detected)(self.flash)

        # DFUHandler emits an (addr, data) stream straight into the flash writer.
        wiring.connect(m, self.dfu.source, flash.i)

        # Manifestation (zero-length final DNLOAD): flush the last page and arm
        # the reload.
        flushing = Signal()
        arm      = Signal()
        with m.If(self.dfu.manifest):
            m.d.sync += [flushing.eq(1), arm.eq(1)]
        m.d.comb += flash.done.eq(flushing)

        # Status: DONE (manifestation) > ACTIVE (writing) > IDLE.
        # TODO: report ERROR
        #   with m.If(self.dfu.state == DFUState.dfuERROR.value):
        #       m.d.comb += self.status.eq(Status.ERROR)
        with m.If(arm):
            m.d.comb += self.status.eq(Status.DONE)
        with m.Elif(self.dfu.source.valid | flash.qo.valid):
            m.d.comb += self.status.eq(Status.ACTIVE)

        self.qo = flash.qo
        self.qi = flash.qi

        return Reconfig(arm=arm, activity=self.dfu.source.valid | flash.qo.valid)
