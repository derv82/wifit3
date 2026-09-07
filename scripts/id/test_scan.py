"""Tests for scripts/id/scan.py. No hardware: the Tracker is driven with crafted Frames."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from frame import Frame, IE   # noqa: E402
from scan import (   # noqa: E402
    Tracker, _harvest_m1, _ssid_for, format_record, format_wps_m1,
)
from test_frame import beacon, ie   # noqa: E402
from wifit3.dot11.wsc import messages as M   # noqa: E402

_RSN_PSK = bytes.fromhex("0100" "000fac04" "0100" "000fac04" "0100" "000fac02" "0c00")


def _frame(*ies: bytes, src: bytes = b"\x02\x00\x00\x00\x00\x01") -> Frame:
    return Frame.parse(beacon(b"".join(ies), src=src))


def test_first_sight_emits_full_record():
    t = Tracker()
    rec = t.observe(_frame(ie(0, b"net"), ie(1, bytes([0x82]))))
    assert rec.kind == "new"
    assert rec.src == "02:00:00:00:00:01"
    assert rec.name == "beacon"
    assert set(rec.ies) == {IE.SSID, IE.SUPP_RATES}


def test_identical_second_frame_emits_nothing():
    t = Tracker()
    assert t.observe(_frame(ie(0, b"net"))) is not None
    assert t.observe(_frame(ie(0, b"net"))) is None


def test_changed_ie_emits_diff_naming_that_ie():
    t = Tracker()
    t.observe(_frame(ie(0, b"net")))
    rec = t.observe(_frame(ie(0, b"NEW")))
    assert rec.kind == "diff"
    assert set(rec.changed) == {IE.SSID}
    assert set(rec.added) == set()
    assert rec.changed[IE.SSID] == (b"net", b"NEW")


def test_added_ie_emits_under_added():
    t = Tracker()
    t.observe(_frame(ie(0, b"net")))
    rec = t.observe(_frame(ie(0, b"net"), ie(48, _RSN_PSK)))
    assert rec.kind == "diff"
    assert set(rec.added) == {IE.RSN}
    assert set(rec.changed) == set()


def test_missing_ie_is_not_a_removal():
    t = Tracker()
    t.observe(_frame(ie(0, b"net"), ie(48, _RSN_PSK)))
    # a frame carrying only SSID (RSN absent) is unchanged: accumulated state keeps RSN.
    assert t.observe(_frame(ie(0, b"net"))) is None


def test_different_sources_tracked_independently():
    t = Tracker()
    a = t.observe(_frame(ie(0, b"net"), src=b"\x02\x00\x00\x00\x00\x01"))
    b = t.observe(_frame(ie(0, b"net"), src=b"\x02\x00\x00\x00\x00\x02"))
    assert a.kind == "new" and b.kind == "new"
    assert t.devices == 2


def test_different_frame_names_tracked_independently():
    t = Tracker()
    probe = Frame.parse(b"\x40\x00" + b"\x00\x00" + b"\xff" * 6
                        + b"\x02\x00\x00\x00\x00\x01" + b"\x00" * 6 + b"\x00\x00" + ie(0, b"net"))
    assert probe.name == "probe_req"
    assert t.observe(_frame(ie(0, b"net"))).name == "beacon"
    assert t.observe(probe).kind == "new"   # same src, different frame name: new key


def test_empty_ies_frame_ignored():
    t = Tracker()
    assert t.observe(_frame()) is None       # beacon with no allowlisted IEs
    assert t.observe(None) is None


def test_mac_filter_screens_out_other_sources():
    t = Tracker(mac_filter="02:00:00:00:00:09")
    assert t.observe(_frame(ie(0, b"net"))) is None
    assert t.observe(_frame(ie(0, b"net"), src=b"\x02\x00\x00\x00\x00\x09")) is not None


def test_ssid_going_blank_is_ignored_and_name_kept():
    t = Tracker()
    assert t.observe(_frame(ie(0, b"Rai5"))) is not None
    assert t.observe(_frame(ie(0, b""))) is None
    assert t.stored[("02:00:00:00:00:01", "beacon")][IE.SSID] == b"Rai5"


def test_ssid_reappears_after_blank_flap_still_deduped():
    t = Tracker()
    t.observe(_frame(ie(0, b"Rai5")))
    t.observe(_frame(ie(0, b"")))
    assert t.observe(_frame(ie(0, b"Rai5"))) is None


def test_ssid_rename_still_emits_a_diff():
    t = Tracker()
    t.observe(_frame(ie(0, b"Rai5")))
    rec = t.observe(_frame(ie(0, b"Other")))
    assert rec.kind == "diff"
    assert set(rec.changed) == {IE.SSID}
    assert rec.changed[IE.SSID] == (b"Rai5", b"Other")


def test_blank_then_named_is_a_real_change():
    t = Tracker()
    assert t.observe(_frame(ie(0, b""))).kind == "new"
    rec = t.observe(_frame(ie(0, b"Named")))
    assert rec.kind == "diff"
    assert rec.changed[IE.SSID] == (b"", b"Named")


def test_blank_ssid_dropped_but_simultaneous_change_kept():
    t = Tracker()
    t.observe(_frame(ie(0, b"Rai5"), ie(1, bytes([0x82]))))
    rec = t.observe(_frame(ie(0, b""), ie(1, bytes([0x84]))))
    assert rec.kind == "diff"
    assert set(rec.changed) == {IE.SUPP_RATES}
    assert t.stored[("02:00:00:00:00:01", "beacon")][IE.SSID] == b"Rai5"


def test_format_record_new():
    t = Tracker()
    rec = t.observe(_frame(ie(0, b"net")))
    out = format_record(rec)
    assert out.splitlines()[0] == "NEW 02:00:00:00:00:01 [beacon]"
    assert "    SSID='net'" in out


def test_format_record_diff():
    t = Tracker()
    t.observe(_frame(ie(0, b"net")))
    rec = t.observe(_frame(ie(0, b"NEW"), ie(48, _RSN_PSK)))
    out = format_record(rec)
    assert out.splitlines()[0] == "~ 02:00:00:00:00:01 [beacon]"
    assert "    + RSN=" in out
    assert "    * SSID: 'net' -> 'NEW'" in out


# --- WPS M1 harvest (no hardware: a fake transport replays crafted EAP/WSC frames) --------
_BSSID = b"\xaa\xbb\xcc\xdd\xee\xff"
_OURMAC = b"\x02\x00\x00\x00\x00\x01"


class _FakeTransport:
    """Replays a scripted list of RX frames; records what the harvester sends."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def send_no_wait(self, frame):
        self.sent.append(frame)
        return True

    async def recv(self, timeout):
        return self._frames.pop(0) if self._frames else None


def _identity_request(eap_id=1):
    payload = M._eapol_eap(M.EAP_REQUEST, eap_id, bytes([M.EAP_TYPE_IDENTITY]))
    return M.build_data_frame(_BSSID, _BSSID, _OURMAC, payload)


def _m1_frame(eap_id=2):
    attrs = (M.tlv(M.ATTR_MSG_TYPE, bytes([M.WPS_M1]))
             + M.tlv(M.ATTR_MANUFACTURER, b"Acme")
             + M.tlv(M.ATTR_MODEL_NAME, b"Router9000")
             + M.tlv(M.ATTR_DEV_NAME, b"AP-Lobby"))
    return M.build_data_frame(_BSSID, _BSSID, _OURMAC, M.eap_wsc_response(eap_id, M.WSC_MSG, attrs))


async def test_harvest_m1_answers_identity_and_returns_attrs():
    t = _FakeTransport([_identity_request(1), _m1_frame(2)])
    attrs = await _harvest_m1(t, _BSSID, _OURMAC, timeout=0.01)
    assert attrs is not None
    assert attrs[M.ATTR_MANUFACTURER] == b"Acme"
    assert attrs[M.ATTR_DEV_NAME] == b"AP-Lobby"
    assert len(t.sent) == 2                     # EAPOL-Start, then the identity response


async def test_harvest_m1_returns_none_on_silence():
    t = _FakeTransport([])                       # AP never answers
    assert await _harvest_m1(t, _BSSID, _OURMAC, tries=3, timeout=0.01) is None


def test_format_wps_m1_line():
    attrs = {M.ATTR_MANUFACTURER: b"Acme", M.ATTR_MODEL_NAME: b"R9000", M.ATTR_UUID_E: b"\x00\x11"}
    line = format_wps_m1("aa:bb:cc:dd:ee:ff", attrs)
    assert line.startswith("WPS-M1 aa:bb:cc:dd:ee:ff ")
    assert "mfr=Acme" in line and "model=R9000" in line
    assert "uuid=0011" in line                   # non-printable value renders as hex


def test_ssid_for_reads_tracker_beacon():
    t = Tracker("02:00:00:00:00:01")
    t.observe(_frame(ie(0, b"TestNet")))
    assert _ssid_for(t, "02:00:00:00:00:01") == "TestNet"
    assert _ssid_for(t, "aa:bb:cc:dd:ee:ff") == ""
