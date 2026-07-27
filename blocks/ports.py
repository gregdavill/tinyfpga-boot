from amaranth.lib import io


__all__ = ["PortGroup", "X1QuadDq"]


class PortGroup:
    """Group of Amaranth library I/O ports.

    This object is a stand-in for the object returned by the Amaranth :py:`platform.request()`
    function, as expected by the I/O cores.
    """

    def __init__(self, **kwargs):
        self._ports_ = kwargs

    def __getitem__(self, name):
        return self._ports_[name]

    def __setitem__(self, name, port):
        assert port is None or isinstance(port, io.PortLike), f"cannot assign {port!r}"
        self._ports_[name] = port

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name=name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = value

    def __iter__(self):
        for name, port in self._ports_.items():
            yield name, port


# HACK: temporary until RFC #79
import copy
def SimulationPort_with_direction(self, direction):
    self = copy.copy(self)
    self._direction = io.Direction(direction)
    return self
io.SimulationPort.with_direction = SimulationPort_with_direction


class X1QuadDq:
    """Adapt an x1 SPI flash w/ COPI/CIPO pins, to a quad `Controller`'s 4-lane `dq`."""

    direction = io.Direction.Bidir

    def __init__(self, copi, cipo):
        self._lanes = [self._bidir(copi), self._bidir(cipo),
                       io.SimulationPort("io", 1), io.SimulationPort("io", 1)]

    @staticmethod
    def _bidir(port):
        # Real hardware pads are SingleEndedPort; Sim hands us SimulationPorts.
        if isinstance(port, io.SingleEndedPort):
            return io.SingleEndedPort(port.io, direction=io.Direction.Bidir)
        return port.with_direction(io.Direction.Bidir)

    def __len__(self):
        return len(self._lanes)

    def __getitem__(self, index):
        return self._lanes[index]