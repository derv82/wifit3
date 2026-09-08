from __future__ import annotations

from unittest.mock import MagicMock

from wifit3.campaigns.probe import BaseApProbe, ProbeResult, probe_ap
from wifit3.campaigns.probe.mikrotik import MikrotikProbe
from wifit3.campaigns.probe.ubiquiti import UbiquitiProbe
from wifit3.campaigns.probe.wps_m1 import WpsM1Probe
from wifit3.models import AccessPoint, IdKey, IdSource


async def test_probe_ap_runs_applicable_probes():
    class DummyProbe(BaseApProbe):
        name = "dummy"

        def can_probe(self, ap: AccessPoint) -> bool:
            return True

        async def probe(self, iface, ap: AccessPoint) -> ProbeResult:
            ap.identity.set(IdSource.WINBOX_PROBE, IdKey.MANUFACTURER, "Dummy")
            return ProbeResult(True, source="dummy", vendor="Dummy")

    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    iface = MagicMock()
    result = await probe_ap(iface, ap, probes=(DummyProbe(),))

    assert result.ok is True
    assert result.source == "dummy"
    assert result.vendor == "Dummy"
    assert ap.identity.manufacturer == "Dummy"


async def test_probe_ap_rejects_when_no_suitable_probes():
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6, encryption="WPA2", wps=False)
    iface = MagicMock()
    result = await probe_ap(iface, ap)

    assert result.ok is False
    assert "no suitable probes" in result.detail


def test_probe_gates():
    open_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", encryption="OPEN", wps=False)
    wpa_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", encryption="WPA2", wps=False)
    wps_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", encryption="WPA2", wps=True)

    assert MikrotikProbe().can_probe(open_ap) is True
    assert MikrotikProbe().can_probe(wpa_ap) is False

    assert UbiquitiProbe().can_probe(open_ap) is True
    assert UbiquitiProbe().can_probe(wpa_ap) is False

    assert WpsM1Probe().can_probe(wps_ap) is True
    assert WpsM1Probe().can_probe(wpa_ap) is False
