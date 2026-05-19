"""Thin wrappers around LUNA's stream endpoints

Bridge LUNA custom streams into more modern amaranth.lib.stream

LUNA's `USBStreamOutEndpoint` / `USBStreamInEndpoint` track 
`expected_data_toggle` internally and reset it only on 
`ClearFeature(ENDPOINT_HALT)`. 
USB 2.0 §9.4.5 the toggle must also be reset on every successful
SET_CONFIGURATION; after a USB bus reset, the host's DATA0 first 
packet of the next transfer may silently be discarded as a stale
retransmission.

Expose `.interface` so that `USBDevice.add_endpoint()` plugs them
into the endpoint multiplexer unchanged.
"""

from amaranth import Module, Signal, ResetInserter
from amaranth.lib import data, wiring, stream
from amaranth.lib.wiring import In, Out

from luna import usb2


_stream_layout = data.StructLayout({"data": 8, "first": 1, "last": 1})


def _config_change_pulse(m, active_config: Signal) -> Signal:
    """Returns a 1-cycle pulse on every transition of `active_config`.
    Used as the reset trigger for the wrapped LUNA endpoints; fires
    on the device-state register's 1→0 drop at bus reset AND the
    0→N rise at the next SET_CONFIGURATION."""
    prev = Signal.like(active_config)
    pulse = Signal()
    m.d.usb  += prev.eq(active_config)
    m.d.comb += pulse.eq(prev != active_config)
    return pulse


class USBStreamInEndpoint(wiring.Component):
    def __init__(self, *, endpoint_number, max_packet_size):
        super().__init__({
            "i": In(stream.Signature(_stream_layout)),
        })
        self._inner = usb2.USBStreamInEndpoint(
            endpoint_number=endpoint_number,
            max_packet_size=max_packet_size,
        )
        # Proxy onto LUNA's EndpointInterface so `USBDevice.add_endpoint`
        # and its multiplexer see this wrapper as a regular LUNA
        # endpoint.
        self.interface = self._inner.interface

    def elaborate(self, platform):
        m = Module()
        reset_pulse = _config_change_pulse(m, self.interface.active_config)
        m.submodules.inner = ResetInserter({"usb": reset_pulse})(self._inner)

        m.d.comb += [
            self._inner.stream.payload.eq(self.i.p.data),
            self._inner.stream.first.eq(self.i.p.first),
            self._inner.stream.last.eq(self.i.p.last),
            self._inner.stream.valid.eq(self.i.valid),
            self.i.ready.eq(self._inner.stream.ready),
        ]
        return m


class USBStreamOutEndpoint(wiring.Component):
    def __init__(self, *, endpoint_number, max_packet_size):
        super().__init__({
            "o": Out(stream.Signature(_stream_layout)),
        })
        self._inner = usb2.USBStreamOutEndpoint(
            endpoint_number=endpoint_number,
            max_packet_size=max_packet_size,
        )
        self.interface = self._inner.interface

    def elaborate(self, platform):
        m = Module()
        reset_pulse = _config_change_pulse(m, self.interface.active_config)
        m.submodules.inner = ResetInserter({"usb": reset_pulse})(self._inner)

        m.d.comb += [
            self.o.p.data.eq(self._inner.stream.payload),
            self.o.p.first.eq(self._inner.stream.first),
            self.o.p.last.eq(self._inner.stream.last),
            self.o.valid.eq(self._inner.stream.valid),
            self._inner.stream.ready.eq(self.o.ready),
        ]
        return m
