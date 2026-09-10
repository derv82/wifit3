import asyncio
from unittest.mock import MagicMock

from wifit3.campaigns.probe import BaseApProbe, ProbeResult, probe_ap
from wifit3.campaigns.probe.wps_m1 import WpsM1Probe, _trigger_m1
from wifit3.models import AccessPoint, IdKey, IdSource


async def test_probe_ap_runs_applicable_probes():
    class DummyProbe(BaseApProbe):
        name = "dummy"

        def can_probe(self, ap: AccessPoint) -> bool:
            return True

        async def probe(self, iface, ap: AccessPoint) -> ProbeResult:
            ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Dummy")
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
    wpa_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", encryption="WPA2", wps=False)
    wps_ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", encryption="WPA2", wps=True)

    assert WpsM1Probe().can_probe(wps_ap) is True
    assert WpsM1Probe().can_probe(wpa_ap) is False


async def test_trigger_m1_times_out_cleanly():
    class SilentTransport:
        def __init__(self):
            self.sent = []

        async def send_no_wait(self, frame):
            self.sent.append(frame)
            return True

        async def recv(self, timeout):
            await asyncio.sleep(0.01)
            return None

    transport = SilentTransport()
    bssid = b"\x11\x22\x33\x44\x55\x66"
    our_mac = b"\xaa\xbb\xcc\xdd\xee\xff"
    res = await _trigger_m1(transport, bssid, our_mac, resend_interval=0.02, max_resends=2, total_timeout=0.08)
    assert res is None
    # Verify it attempted to resend when silent
    assert len(transport.sent) >= 2
