"""manager delegates enumeration and lookup to the platform backend."""
import wifit3.device.manager as manager


class _FakeBackend:
    def __init__(self, devs=()):
        self.devs = list(devs)

    def enumerate(self):
        return list(self.devs)

    def find(self, vid, pid, bus=None, address=None):
        for dev in self.devs:
            if (dev.idVendor, dev.idProduct) == (vid, pid):
                return dev
        return None

    def find_all(self, vid, pid):
        return [d for d in self.devs if (d.idVendor, d.idProduct) == (vid, pid)]


def _dev(vid=0x148F, pid=0x5370, bus=1, address=2):
    return type("D", (), {"idVendor": vid, "idProduct": pid,
                          "bus": bus, "address": address})()


def test_devices_empty(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: _FakeBackend())
    assert manager.devices() == []


def test_devices_uses_the_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: _FakeBackend([_dev()]))
    out = manager.devices()
    assert [d.chipset for d in out] == ["RT5370"]
    assert (out[0].bus, out[0].address) == (1, 2)
