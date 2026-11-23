"""Shared fake UsbBackend for tests."""
from typing import Any, Dict, Iterable, List, Optional

from wifit3.device.backend import UsbBackend


class FakeBackend(UsbBackend):
    """A UsbBackend over a fixed device list, recording the last find() target."""

    def __init__(self, devs: Iterable[Any] = ()) -> None:
        self.devs = list(devs)
        self.seen: Dict[str, Optional[int]] = {}

    def enumerate(self) -> List[Any]:
        return list(self.devs)

    def find(self, vid: int, pid: int, bus: Optional[int] = None,
             address: Optional[int] = None) -> Optional[Any]:
        self.seen = {"bus": bus, "address": address}
        for dev in self.devs:
            if (dev.idVendor, dev.idProduct) != (vid, pid):
                continue
            if bus is not None and address is not None and (dev.bus, dev.address) != (bus, address):
                continue
            return dev
        return None

    def find_all(self, vid: int, pid: int) -> List[Any]:
        return [d for d in self.devs if (d.idVendor, d.idProduct) == (vid, pid)]

    def node_path(self, dev: Any) -> Optional[str]:
        return f"/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}"
