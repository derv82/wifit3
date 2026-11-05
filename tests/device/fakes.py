"""Shared fake UsbBackend for tests."""


class FakeBackend:
    """A UsbBackend over a fixed device list, recording the last find() target."""

    def __init__(self, devs=()):
        self.devs = list(devs)
        self.seen = {}

    def enumerate(self):
        return list(self.devs)

    def find(self, vid, pid, bus=None, address=None):
        self.seen = {"bus": bus, "address": address}
        for dev in self.devs:
            if (dev.idVendor, dev.idProduct) != (vid, pid):
                continue
            if bus is not None and address is not None and (dev.bus, dev.address) != (bus, address):
                continue
            return dev
        return None

    def find_all(self, vid, pid):
        return [d for d in self.devs if (d.idVendor, d.idProduct) == (vid, pid)]

    def node_path(self, dev):
        return f"/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}"
