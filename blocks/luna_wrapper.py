from amaranth.lib import data, wiring, stream
from amaranth.lib.wiring import In, Out

from luna import usb2
_stream_layout = data.StructLayout({"data": 8, "first": 1, "last": 1})


class USBStreamInEndpoint(usb2.USBStreamInEndpoint, wiring.Component):
    def __init__(self, *, endpoint_number, max_packet_size):
        wiring.Component.__init__(self, {
            "i": In(stream.Signature(_stream_layout))
        })
        usb2.USBStreamInEndpoint.__init__(self, endpoint_number=endpoint_number, max_packet_size=max_packet_size)

    def elaborate(self, platform):
        m = super().elaborate(platform)

        m.d.comb += [
            self.stream.payload.eq(self.i.p.data),
            self.stream.first.eq(self.i.p.first),
            self.stream.last.eq(self.i.p.last),
            self.stream.valid.eq(self.i.valid),
            self.i.ready.eq(self.stream.ready)
        ]
        return m

class USBStreamOutEndpoint(usb2.USBStreamOutEndpoint, wiring.Component):
    def __init__(self, *, endpoint_number, max_packet_size):
        wiring.Component.__init__(self, {
            "o": Out(stream.Signature(_stream_layout))
        })
        usb2.USBStreamOutEndpoint.__init__(self, endpoint_number=endpoint_number, max_packet_size=max_packet_size)

    def elaborate(self, platform):
        m = super().elaborate(platform)

        m.d.comb += [
            self.o.p.data.eq(self.stream.payload),
            self.o.p.first.eq(self.stream.first),
            self.o.p.last.eq(self.stream.last),
            self.o.valid.eq(self.stream.valid),
            self.stream.ready.eq(self.o.ready)
        ]
        return m