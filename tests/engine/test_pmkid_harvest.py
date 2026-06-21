"""PMKID harvest: deauth-on-M1 + the empty-M1 no-retry behaviour.

M1 is terminal for the harvest (we can't compute M2's MIC without the PSK), so on
ANY M1 we send a leaving-deauth (a 3x burst — the path runs without active-monitor,
so our TX is un-ACKed) and stop: with the PMKID on success, empty-handed on a
PMKID-less M1 (no retry — the same AP would only re-send the same empty M1). We only
rotate the MAC + retry when the AP stays silent (no M1).
"""
from types import SimpleNamespace

from wifit3.engine.attacks.pmkid_harvest import PmkidHarvestAttack

_BSSID = "aa:bb:cc:dd:ee:01"
_BSSID_B = bytes.fromhex("aabbccddee01")


def _target(pmf_required=False, pmf_capable=False):
    return SimpleNamespace(bssid=_BSSID, channel=36, ssid="TESTNET", rsn_ie=None,
                           pmf_required=pmf_required, pmf_capable=pmf_capable)


class _FakeIface:
    """Records injected frames; optionally drops an M1 into the handshake dict the
    instant the Assoc Req is sent (simulating the AP's reply)."""

    def __init__(self, deliver_m1: bool, pmkid=None):
        self._deliver_m1 = deliver_m1
        self._pmkid = pmkid
        self.current_channel = 36
        self.ap = SimpleNamespace(handshakes={})
        self.access_points = {_BSSID: self.ap}
        self.sent: list = []

    def register_forged_mac(self, mac):
        pass

    async def set_channel(self, ch):
        self.current_channel = ch

    async def send_raw(self, frame: bytes, use_no_ack: bool = True) -> bool:
        self.sent.append(bytes(frame))
        # Assoc Req (mgmt subtype 0 -> fc0 == 0x00) -> the AP "replies" with M1.
        if self._deliver_m1 and frame[0] == 0x00:
            src = ":".join(f"{b:02x}" for b in frame[10:16])   # addr2 = our forged MAC
            self.ap.handshakes[src] = SimpleNamespace(pmkid=self._pmkid, akm_client=None)
        return True


def _deauths(iface):
    return [f for f in iface.sent if f[0] == 0xC0]             # subtype DEAUTH


def _assoc_reqs(iface):
    return [f for f in iface.sent if f[0] == 0x00]             # subtype ASSOC_REQ


async def test_success_returns_pmkid_and_bursts_deauth():
    pmkid = bytes(range(16))
    iface = _FakeIface(deliver_m1=True, pmkid=pmkid)
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(m1_timeout=0.05)
    assert out == pmkid
    assert a.fail_reason is None
    assert len(_deauths(iface)) == 3                           # 3x leaving-deauth
    assert len(_assoc_reqs(iface)) == 1                        # no retry on success


async def test_empty_m1_deauths_does_not_retry_and_says_why():
    iface = _FakeIface(deliver_m1=True, pmkid=None)
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(attempts=3, m1_timeout=0.05)
    assert out is None
    assert "no PMKID KDE" in a.fail_reason                     # specific, definitive reason
    assert len(_deauths(iface)) == 3                           # still deauth — we got M1
    assert len(_assoc_reqs(iface)) == 1                        # the fix: ONE attempt, not 3


async def test_silent_ap_retries_then_gives_up_without_deauth():
    iface = _FakeIface(deliver_m1=False)
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(attempts=3, m1_timeout=0.02)
    assert out is None
    assert "never answered" in a.fail_reason
    assert len(_assoc_reqs(iface)) == 3                        # rotate + retry while silent
    assert _deauths(iface) == []                               # never got M1 → nothing to leave


async def test_pmf_required_short_circuits_without_tx():
    iface = _FakeIface(deliver_m1=False)
    a = PmkidHarvestAttack(iface, _target(pmf_required=True))
    out = await a.run()
    assert out is None
    assert "PMF Required" in a.fail_reason
    assert iface.sent == []                                    # don't even try — no auth/assoc/deauth


def test_build_deauth_frame():
    a = PmkidHarvestAttack(_FakeIface(deliver_m1=False), _target())
    f = a._build_deauth()
    assert f[0] == 0xC0 and f[1] == 0x00                       # mgmt, subtype DEAUTH
    assert f[4:10] == _BSSID_B                                 # addr1 = AP
    assert f[10:16] == a.source_mac                            # addr2 = us
    assert f[16:22] == _BSSID_B                                # addr3 = AP
    assert f[24:26] == b"\x03\x00"                             # reason 3 = STA leaving
    assert len(f) == 26
