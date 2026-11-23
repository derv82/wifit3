import asyncio
import struct
from unittest.mock import MagicMock

from wifit3.campaigns.probe import BaseApProbe, ProbeResult, probe_ap
from wifit3.campaigns.probe.wps_m1 import WpsM1Probe, _trigger_m1
from wifit3.dot11.wsc import messages as M
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


async def test_trigger_m1_nacks_m1_and_waits_for_eap_failure():
    bssid = b"\x11\x22\x33\x44\x55\x66"
    our_mac = b"\xaa\xbb\xcc\xdd\xee\xff"
    nonce_e = bytes(range(16))

    def ap_frame(eap: bytes) -> bytes:
        x = struct.pack(">BBH", 1, 0, len(eap)) + eap
        return b"\x08\x02\x00\x00" + our_mac + bssid + bssid + b"\x00\x00" + M.LLC_SNAP_EAPOL + x

    m1_attrs = (M.tlv(M.ATTR_VERSION, b"\x10") + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
                + M.tlv(M.ATTR_ENROLLEE_NONCE, nonce_e) + M.tlv(M.ATTR_MANUFACTURER, b"Acme"))
    exp = (bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID + M.WFA_VENDOR_TYPE_SIMPLECONFIG
           + bytes([M.WSC_MSG, 0x00]) + m1_attrs)
    m1 = ap_frame(struct.pack(">BBH", M.EAP_REQUEST, 7, 4 + len(exp)) + exp)
    eap_failure = ap_frame(struct.pack(">BBH", M.EAP_FAILURE, 7, 4))

    class ApTransport:
        def __init__(self):
            self.sent = []
            self.replies = [m1, eap_failure]

        async def send_no_wait(self, frame):
            self.sent.append(frame)
            return True

        async def recv(self, timeout):
            return self.replies.pop(0) if self.replies else None

    transport = ApTransport()
    attrs = await _trigger_m1(transport, bssid, our_mac)

    assert attrs[M.ATTR_MANUFACTURER] == b"Acme"
    nack = M.parse_rx_frame(transport.sent[-1])
    assert nack.eap_code == M.EAP_RESPONSE and nack.eap_id == 7
    assert nack.wsc_opcode == M.WSC_NACK
    assert nack.attrs[M.ATTR_ENROLLEE_NONCE] == nonce_e
    assert transport.replies == []
