"""Front-panel button stay source.

Holding the board's user button at power-on keeps the bootloader resident.
"""

from amaranth import Module, Signal, Cat

from . import StaySource


class ButtonStaySource(StaySource):
    needs_flash = False

    def __init__(self, *, resource="button", number=0):
        self._resource = resource
        self._number = number
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Purely combinational: no flash, always "ready".
        m.d.comb += self.done.eq(1)

        btn = platform.request(self._resource, self._number)

        sync = Signal(2)
        m.d.sync += sync.eq(Cat(btn.i, sync[0]))
        m.d.comb += self.stay.eq(sync[1])

        return m
