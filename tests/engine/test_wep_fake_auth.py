"""Tests for WEP fake authentication (frame builders + RX state machine)."""
import asyncio
import struct

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.fake_auth import WepFakeAuth

SELF_MAC = b"\x02\x00\x00\x00\x00\x01"
BSSID_BYTES = b"\x11\x22\x33\x44\x55\x66"


def _fa(mocker, **kw):
    ap = AccessPoint(
        bssid="11:22:33:44:55:66", ssid="WepNet", channel=6, encryption="WEP"
    )
    return WepFakeAuth(mocker.MagicMock(), ap, source_mac=SELF_MAC, **kw)


def _walk_tag_ids(tags: bytes) -> list[int]:
    ids, i = [], 0
    while i + 2 <= len(tags):
        ids.append(tags[i])
        i += 2 + tags[i + 1]
    return ids


# ---- Frame builders --------------------------------------------------------

def test_auth_req_is_open_system_seq1(mocker):
    f = _fa(mocker)._build_auth_req()
    assert f[0:2] == b"\xb0\x00"            # mgmt / Auth subtype
    assert f[4:10] == BSSID_BYTES           # Addr1 = BSSID
    assert f[10:16] == SELF_MAC             # Addr2 = us
    # Body: algo=0 (Open), seq=1, status=0.
    assert f[24:30] == b"\x00\x00\x01\x00\x00\x00"


def test_assoc_req_sets_privacy_and_omits_rsn(mocker):
    f = _fa(mocker)._build_assoc_req()
    assert f[0:2] == b"\x00\x00"            # mgmt / Assoc Req subtype
    cap = struct.unpack("<H", f[24:26])[0]
    assert cap & 0x0010                     # Privacy bit (mandatory for WEP)
    # Tagged params start after cap(2) + listen interval(2) = offset 28.
    ids = _walk_tag_ids(f[28:])
    assert 0 in ids                         # SSID
    assert 1 in ids                         # Supported Rates
    assert 48 not in ids                    # NO RSN IE — WEP predates RSN


def test_assoc_req_carries_ssid(mocker):
    f = _fa(mocker)._build_assoc_req()
    assert b"WepNet" in f


# ---- RX state machine ------------------------------------------------------

def _mgmt(subtype: int, dest: bytes, body: bytes = b"") -> bytes:
    fc0 = (subtype << 4) & 0xF0            # type=mgmt(0)
    return bytes([fc0, 0x00]) + b"\x00\x00" + dest + BSSID_BYTES + BSSID_BYTES + b"\x00\x00" + body


def _assoc_resp(dest: bytes, status: int) -> bytes:
    body = struct.pack("<H", 0x0011) + struct.pack("<H", status) + b"\x01\xc0"
    return _mgmt(0x01, dest, body)


def _auth_resp(dest: bytes, status: int) -> bytes:
    body = b"\x00\x00" + b"\x02\x00" + struct.pack("<H", status)
    return _mgmt(0x0B, dest, body)


def _deauth(dest: bytes) -> bytes:
    return _mgmt(0x0C, dest, b"\x07\x00")


def test_rx_assoc_resp_success_marks_associated(mocker):
    fa = _fa(mocker)
    fa._active = True
    fa._rx_cb(_assoc_resp(SELF_MAC, status=0), -40, 0.0)
    assert fa._assoc_ok is True


def test_rx_assoc_resp_rejected_records_reason(mocker):
    fa = _fa(mocker)
    fa._active = True
    fa._rx_cb(_assoc_resp(SELF_MAC, status=12), -40, 0.0)
    assert fa._assoc_ok is False
    assert "status 12" in fa.fail_reason


def test_rx_ignores_frames_for_other_macs(mocker):
    fa = _fa(mocker)
    fa._active = True
    fa._rx_cb(_assoc_resp(b"\xaa\xaa\xaa\xaa\xaa\xaa", status=0), -40, 0.0)
    assert fa._assoc_ok is False


def test_rx_auth_reject_records_reason(mocker):
    fa = _fa(mocker)
    fa._active = True
    fa._rx_cb(_auth_resp(SELF_MAC, status=1), -40, 0.0)
    assert "Auth rejected" in fa.fail_reason


def test_rx_deauth_triggers_reactive_reauth(mocker):
    fa = _fa(mocker)
    fa._active = True
    fa.state = "associated"
    fa._rx_cb(_deauth(SELF_MAC), -40, 0.0)
    assert fa._reauth_event.is_set()
    assert fa.state == "authenticating"


# ---- Lifecycle wiring ------------------------------------------------------

async def test_start_stop_wiring(mocker):
    iface = mocker.MagicMock()
    iface.send_raw = mocker.AsyncMock(return_value=True)
    iface.set_channel = mocker.AsyncMock(return_value=True)
    iface.current_channel = 6
    ap = AccessPoint(
        bssid="11:22:33:44:55:66", ssid="WepNet", channel=6, encryption="WEP"
    )
    fa = WepFakeAuth(iface, ap, source_mac=SELF_MAC, assoc_timeout=0.05)

    fa.start()
    assert fa.is_active
    iface.register_self_mac.assert_called_once()
    iface.register_rx_callback.assert_called_once()

    await asyncio.sleep(0)   # let the keepalive task start
    fa.stop()
    assert not fa.is_active
    iface.unregister_rx_callback.assert_called_once()
    iface.unregister_self_mac.assert_called_once()
