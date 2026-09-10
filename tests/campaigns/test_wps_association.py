"""Association: wait for the Auth Resp before the Assoc Req.

An AP drops an Assoc from a not-yet-authenticated STA, so the old blind 0.1s gap
raced cold/slow APs and whiffed first contact (the ~50/50 first-WPS-PBC timeout).
associate() now waits for the Open-System Auth Resp (status 0), then sends Assoc,
falling back to sending it anyway if no matchable Auth Resp arrives.
"""
import struct
from wifit3.dot11.parser import WlanFrameParser

from wifit3.campaigns.auth_assoc import Association

_BSSID = "34:21:09:00:01:ff"
_BSSID_B = bytes.fromhex("3421090001ff")
_US = bytes.fromhex("02aabbccddee")


def _auth_resp(status: int = 0) -> bytes:
    # mgmt/auth (0xB0); addr1=us, addr2/3=AP; body: algo, seq=2, status (@28:30).
    return (b"\xb0\x00\x00\x00" + _US + _BSSID_B + _BSSID_B + b"\x00\x00"
            + b"\x00\x00" + b"\x02\x00" + struct.pack("<H", status))


def _assoc_resp(status: int = 0) -> bytes:
    # mgmt/assoc-resp (0x10); addr1=us; body: cap, status (@26:28), aid.
    return (b"\x10\x00\x00\x00" + _US + _BSSID_B + _BSSID_B + b"\x00\x00"
            + b"\x00\x00" + struct.pack("<H", status) + b"\x01\x00")


class _RespIface:
    """Replies to our auth_req with an Auth Resp and our assoc_req with an Assoc
    Resp, by invoking the registered rx callback (the AP 'answering')."""

    def __init__(self, *, answer_auth: bool = True, answer_assoc: bool = True):
        self.current_channel = 1
        self._cb = None
        self._answer_auth = answer_auth
        self._answer_assoc = answer_assoc
        self.sent = []

    def register_rx_callback(self, cb):
        self._cb = cb

    def unregister_rx_callback(self, cb):
        self._cb = None

    async def set_channel(self, ch):
        self.current_channel = ch

    async def send_no_wait(self, frame, *, use_no_ack=True):
        return await self.send_raw(frame, use_no_ack=use_no_ack)

    async def send_raw(self, frame, use_no_ack=True):
        self.sent.append(bytes(frame))
        subtype = (frame[0] & 0xF0) >> 4
        if subtype == 0x0B and self._answer_auth and self._cb:        # auth req
            self._cb(WlanFrameParser.parse_80211_frame(_auth_resp(), -40))
        elif subtype == 0x00 and self._answer_assoc and self._cb:     # assoc req
            self._cb(WlanFrameParser.parse_80211_frame(_assoc_resp(), -40))
        return True


def _subtypes(iface):
    return [(f[0] & 0xF0) >> 4 for f in iface.sent]


async def test_waits_for_auth_resp_then_sends_assoc():
    iface = _RespIface()
    a = Association(iface, _BSSID, "Net", 1, our_mac=_US)
    a.start()
    assert await a.associate() is True
    assert a._auth_ok and a._assoc_ok
    assert _subtypes(iface) == [0x0B, 0x00]      # one auth, then one assoc; no retries


async def test_falls_back_to_assoc_when_no_auth_resp():
    # AP answers Assoc but not Auth. We still associate, via the auth_timeout fallback.
    iface = _RespIface(answer_auth=False)
    a = Association(iface, _BSSID, "Net", 1, our_mac=_US, auth_timeout=0.05)
    a.start()
    assert await a.associate() is True
    assert a._auth_ok is False                   # never saw an Auth Resp
    assert _subtypes(iface) == [0x0B, 0x00]


def _deauth(reason: int = 6) -> bytes:
    # mgmt/deauth (0xC0); addr1=us, addr2/3=AP; reason (@24:26).
    return (b"\xc0\x00\x00\x00" + _US + _BSSID_B + _BSSID_B + b"\x00\x00"
            + struct.pack("<H", reason))


def _disassoc(reason: int = 7) -> bytes:
    # mgmt/disassoc (0xA0); addr1=us, addr2/3=AP; reason (@24:26).
    return (b"\xa0\x00\x00\x00" + _US + _BSSID_B + _BSSID_B + b"\x00\x00"
            + struct.pack("<H", reason))


def test_rx_cb_sets_auth_ok_on_status0_resp():
    a = Association(_RespIface(), _BSSID, "Net", 1, our_mac=_US)
    a._active = True
    a._rx_cb(WlanFrameParser.parse_80211_frame(_auth_resp(0), -40))
    assert a._auth_ok is True
    a._auth_ok = False
    a._rx_cb(WlanFrameParser.parse_80211_frame(_auth_resp(1), -40))            # status != 0 → not ok, records reason
    assert a._auth_ok is False
    assert "Auth rejected" in (a.fail_reason or "")
    assert "unspecified-failure" in (a.fail_reason or "")


def test_rx_cb_decodes_assoc_rejected_status():
    a = Association(_RespIface(), _BSSID, "Net", 1, our_mac=_US)
    a._active = True
    a._rx_cb(WlanFrameParser.parse_80211_frame(_assoc_resp(18), -40))
    assert a._assoc_ok is False
    assert "basic-rates-unsupported" in (a.fail_reason or "")


def test_rx_cb_decodes_deauth_and_disassoc_reason():
    a = Association(_RespIface(), _BSSID, "Net", 1, our_mac=_US)
    a._active = True
    a.associated = True
    a._rx_cb(WlanFrameParser.parse_80211_frame(_deauth(6), -40))
    assert not a.associated
    assert "deauth" in (a.fail_reason or "")
    assert "class2-from-nonauth" in (a.fail_reason or "")

    a.associated = True
    a._rx_cb(WlanFrameParser.parse_80211_frame(_disassoc(7), -40))
    assert not a.associated
    assert "disassoc" in (a.fail_reason or "")
    assert "class3-from-nonassoc" in (a.fail_reason or "")


def test_assoc_req_rates_and_ht_caps_per_band():
    from wifit3.dot11.auth_assoc import assoc_req
    f_24 = assoc_req(_BSSID_B, _US, "Net", channel=6)
    # 2.4 GHz has DSSS basic rate 0x82 (1 Mbps) and extended rates tag 50
    assert b"\x82" in f_24
    assert b"\x32\x04" in f_24       # Tag 50, len 4
    assert b"\x2d\x1a" in f_24       # Tag 45 (HT Caps), len 26

    f_5g = assoc_req(_BSSID_B, _US, "Net", channel=36)
    # 5 GHz has OFDM basic rate 0x8C (6 Mbps), no tag 50, and HT caps
    assert b"\x8c" in f_5g
    assert b"\x82" not in f_5g
    assert b"\x32" not in f_5g       # No Tag 50 on 5 GHz
    assert b"\x2d\x1a" in f_5g       # Tag 45 (HT Caps), len 26


def test_assoc_req_dynamic_privacy():
    from wifit3.dot11.auth_assoc import assoc_req
    wps_trailer = b"\xdd\x08\x00\x50\xf2\x04\x00\x01\x00\x01"
    rsn_trailer = b"\x30\x14\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x02\x00\x00"

    f_wps = assoc_req(_BSSID_B, _US, "Net", trailer_ies=wps_trailer)
    cap_wps = struct.unpack("<H", f_wps[24:26])[0]
    assert (cap_wps & 0x0010) == 0   # Privacy bit CLEAR for WPS

    f_rsn = assoc_req(_BSSID_B, _US, "Net", trailer_ies=rsn_trailer)
    cap_rsn = struct.unpack("<H", f_rsn[24:26])[0]
    assert (cap_rsn & 0x0010) != 0   # Privacy bit SET for RSN
