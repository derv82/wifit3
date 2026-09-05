from dataclasses import dataclass

from wifit3.campaigns.router_probe import probe_router_info
from wifit3.models import AccessPoint
from wifit3.wlan.router_fingerprint import RouterClaim, RouterEvidence


@dataclass(frozen=True)
class _Result:
    ok: bool
    detail: str = ""
    claims: tuple[RouterClaim, ...] = ()


async def test_router_info_probe_falls_back_to_ubnt(monkeypatch):
    async def fail_mikrotik(array, ap, iface=None):
        return _Result(False, detail="no WinBox response")

    async def find_ubnt(array, ap, iface=None):
        evidence = RouterEvidence("ubnt.discovery", "reachable", "true", 0.99, passive=False)
        return _Result(True, claims=(RouterClaim("vendor", "Ubiquiti", 0.99, (evidence,)),))

    import wifit3.campaigns.router_probe as router_probe

    monkeypatch.setattr(router_probe, "probe_mikrotik", fail_mikrotik)
    monkeypatch.setattr(router_probe, "probe_ubnt", find_ubnt)

    result = await probe_router_info(None, AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6))

    assert result.ok is True
    assert result.source == "ubnt.discovery"
    assert result.claims[0].value == "Ubiquiti"
