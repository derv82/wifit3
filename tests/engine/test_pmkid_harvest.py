"""PMKID harvest: deauth-on-M1 + the empty-M1 no-retry behaviour.

M1 is terminal for the harvest (we can't compute M2's MIC without the PSK), so on
ANY M1 we send a leaving-deauth (a 3x burst — the path runs without active-monitor,
so our TX is un-ACKed) and stop: with the PMKID on success, empty-handed on a
PMKID-less M1 (no retry — the same AP would only re-send the same empty M1). We only
rotate the MAC + retry when the AP stays silent (no M1).
"""
from types import SimpleNamespace

from wifit3.engine.attacks.pmkid_harvest import (
    PmkidFail, PmkidHarvestAttack, _force_psk_akm,
)

_BSSID = "aa:bb:cc:dd:ee:01"
_BSSID_B = bytes.fromhex("aabbccddee01")


def _target(pmf_required=False, pmf_capable=False, akm_suites=(0x02,), rsn_ie=None):
    return SimpleNamespace(bssid=_BSSID, channel=36, ssid="TESTNET", rsn_ie=rsn_ie,
                           pmf_required=pmf_required, pmf_capable=pmf_capable,
                           akm_suites=list(akm_suites))


class _FakeIface:
    """Records injected frames; optionally drops an M1 into the handshake dict the
    instant the Assoc Req is sent (simulating the AP's reply)."""

    def __init__(self, deliver_m1: bool, pmkid=None, fake_mac_supported: bool = False):
        self._deliver_m1 = deliver_m1
        self._pmkid = pmkid
        self._fake_mac_supported = fake_mac_supported
        self.current_channel = 36
        self.ap = SimpleNamespace(handshakes={})
        self.access_points = {_BSSID: self.ap}
        self.sent: list = []
        self.fake_mac_arms = 0
        self.fake_mac_clears = 0

    def register_forged_mac(self, mac):
        pass

    async def set_fake_mac(self, mac, bssid=None):
        if not self._fake_mac_supported:
            return None                              # card lacks FAKE_MAC → un-ACKed
        self.fake_mac_arms += 1
        return ":".join(f"{b:02x}" for b in mac)

    async def clear_fake_mac(self):
        self.fake_mac_clears += 1

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
    assert a.fail_reason is PmkidFail.NO_KDE                   # specific, definitive reason
    assert len(_deauths(iface)) == 3                           # still deauth — we got M1
    assert len(_assoc_reqs(iface)) == 1                        # the fix: ONE attempt, not 3


async def test_silent_ap_retries_then_gives_up_without_deauth():
    iface = _FakeIface(deliver_m1=False)
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(attempts=3, m1_timeout=0.02)
    assert out is None
    assert a.fail_reason is PmkidFail.NO_RESPONSE
    assert len(_assoc_reqs(iface)) == 3                        # rotate + retry while silent
    assert _deauths(iface) == []                               # never got M1 → nothing to leave


async def test_pmf_required_short_circuits_without_tx():
    iface = _FakeIface(deliver_m1=False)
    a = PmkidHarvestAttack(iface, _target(pmf_required=True))
    out = await a.run()
    assert out is None
    assert a.fail_reason is PmkidFail.PMF_REQUIRED
    assert iface.sent == []                                    # don't even try — no auth/assoc/deauth


async def test_no_psk_akm_short_circuits():
    # SAE-only AP (WPA3) → no PSK PMK to harvest → bail before any TX.
    iface = _FakeIface(deliver_m1=True, pmkid=bytes(16))
    a = PmkidHarvestAttack(iface, _target(akm_suites=(0x08,)))   # 0x08 = SAE
    out = await a.run()
    assert out is None
    assert a.fail_reason is PmkidFail.NO_PSK_AKM
    assert iface.sent == []


def test_force_psk_akm_selects_psk_from_sae_first_list():
    # SAE + PSK (SAE listed first) → rewritten to a single PSK AKM.
    rsn = bytes.fromhex("30180100000fac040100000fac040200000fac08000fac020000")
    assert _force_psk_akm(rsn) == bytes.fromhex(
        "30140100000fac040100000fac040100000fac020000")


def test_force_psk_akm_preserves_caps_and_group_mgmt():
    # PSK + RSN caps (PMF bits) + group-mgmt cipher (BIP) → tail untouched.
    rsn = bytes.fromhex("30180100000fac040100000fac040100000fac028c00000fac06")
    assert _force_psk_akm(rsn) == rsn          # already single-PSK; nothing else moves


def test_force_psk_akm_rejects_malformed():
    assert _force_psk_akm(b"\x30\x02\x01\x00") is None         # too short for an AKM list
    assert _force_psk_akm(b"\xdd\x10rubbish!!") is None        # not an RSN IE (tag 0xDD)


async def test_active_monitor_armed_when_supported():
    iface = _FakeIface(deliver_m1=True, pmkid=bytes(range(16)), fake_mac_supported=True)
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(m1_timeout=0.05)
    assert out == bytes(range(16))
    assert iface.fake_mac_arms >= 1            # HW-ACK armed for our forged MAC
    assert iface.fake_mac_clears == 1          # and torn down exactly once at the end


async def test_active_monitor_skipped_when_unsupported():
    # FAKE_MAC unsupported → set_fake_mac returns None → keep going un-ACKed, no clear.
    iface = _FakeIface(deliver_m1=True, pmkid=bytes(range(16)))   # fake_mac_supported=False
    a = PmkidHarvestAttack(iface, _target())
    out = await a.run(m1_timeout=0.05)
    assert out == bytes(range(16))             # still harvests via the un-ACKed fallback
    assert iface.fake_mac_clears == 0          # nothing armed → nothing to clear


def test_build_deauth_frame():
    a = PmkidHarvestAttack(_FakeIface(deliver_m1=False), _target())
    f = a._build_deauth()
    assert f[0] == 0xC0 and f[1] == 0x00                       # mgmt, subtype DEAUTH
    assert f[4:10] == _BSSID_B                                 # addr1 = AP
    assert f[10:16] == a.source_mac                            # addr2 = us
    assert f[16:22] == _BSSID_B                                # addr3 = AP
    assert f[24:26] == b"\x03\x00"                             # reason 3 = STA leaving
    assert len(f) == 26
