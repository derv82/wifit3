"""Tests for the typed auto-save module (engine.save)."""
from __future__ import annotations

from wifit3.engine.models import AccessPoint, EapolFrame, Handshake
from wifit3.engine.save import (
    save_handshake,
    save_pmkid,
    save_wep_key,
    save_wps_pbc,
    save_wps_pin,
)


# ---- Fixtures (mirror tests/engine/test_hc22000.py) ------------------------

def _eapol_payload(mic: bytes = b"\xFF" * 16, key_data_len: int = 0) -> bytes:
    pl = bytearray(99 + key_data_len)
    pl[0] = 0x02
    pl[1] = 0x03
    pl[2:4] = (95 + key_data_len).to_bytes(2, "big")
    pl[4] = 0x02
    pl[5:7] = b"\x00\x8a"
    pl[81:97] = mic
    pl[97:99] = key_data_len.to_bytes(2, "big")
    return bytes(pl)


def _ef(msg_num: int, replay: int = 0, nonce: bytes | None = None,
        mic: bytes | None = None, key_data_len: int = 0,
        payload_mic: bytes | None = None) -> EapolFrame:
    nonce = nonce if nonce is not None else bytes(range(32))
    mic = mic if mic is not None else b"\xAA" * 16
    payload_mic = payload_mic if payload_mic is not None else mic
    return EapolFrame(
        raw=b"\x00" * 24,
        msg_num=msg_num,
        replay_hex=replay.to_bytes(8, "big").hex(),
        nonce=nonce,
        mic=mic,
        key_data_len=key_data_len,
        eapol_payload=_eapol_payload(mic=payload_mic, key_data_len=key_data_len),
    )


def _ap_with_hs(ssid: str = "HomeNet", bssid: str = "aa:bb:cc:dd:ee:ff",
                client_mac: str = "11:22:33:44:55:66",
                *, anonce: bytes | None = None,
                pmkid: bytes | None = None,
                with_pair: bool = True) -> AccessPoint:
    ap = AccessPoint(bssid=bssid, ssid=ssid)
    hs = Handshake(
        bssid=bssid, client_mac=client_mac,
        beacon_frame=b"BEACON", pmkid=pmkid,
    )
    if with_pair:
        anonce = anonce if anonce is not None else bytes(b"\xA0" + b"\x00" * 31)
        m1 = _ef(1, replay=5, nonce=anonce, payload_mic=b"\x00" * 16)
        m2 = _ef(2, replay=5, nonce=b"\xB0" + b"\x00" * 31, key_data_len=22)
        hs.eapol_frames.extend([m1, m2])
    ap.handshakes[client_mac] = hs
    return ap


# ---- save_handshake --------------------------------------------------------

class TestSaveHandshake:
    def test_writes_hc22000_and_pcap(self, tmp_path):
        ap = _ap_with_hs()
        path = save_handshake(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert path is not None
        assert path.name.endswith("_handshake.hc22000")
        assert path.exists()
        pcap = path.with_suffix(".pcap")
        assert pcap.exists() and pcap.stat().st_size > 0

    def test_body_is_wpa02_only(self, tmp_path):
        # AP also has a PMKID — save_handshake must NOT include the WPA*01 line.
        ap = _ap_with_hs(pmkid=b"\x11" * 16)
        path = save_handshake(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "WPA*02*" in text
        assert "WPA*01*" not in text

    def test_dedupes_same_anonce(self, tmp_path):
        ap = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        first = save_handshake(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert first is not None
        # Rebuild with the same ANonce — should dedupe.
        ap2 = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        again = save_handshake(ap2, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert again is None

    def test_different_anonce_writes_new(self, tmp_path):
        ap1 = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        ap2 = _ap_with_hs(anonce=b"\xC0" + b"\x00" * 31)
        save_handshake(ap1, "11:22:33:44:55:66", captures_dir=tmp_path)
        second = save_handshake(ap2, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert second is not None
        files = list(tmp_path.glob("*_handshake.hc22000"))
        assert len(files) == 2

    def test_hidden_ssid_returns_none(self, tmp_path):
        ap = _ap_with_hs(ssid="")
        # Pydantic rejects empty string into Optional[str]; set after construction.
        ap.ssid = None
        assert save_handshake(ap, "11:22:33:44:55:66", captures_dir=tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_no_valid_pair_returns_none(self, tmp_path):
        ap = _ap_with_hs(with_pair=False)
        assert save_handshake(ap, "11:22:33:44:55:66", captures_dir=tmp_path) is None

    def test_unknown_client_returns_none(self, tmp_path):
        ap = _ap_with_hs()
        assert save_handshake(ap, "ff:ff:ff:ff:ff:ff", captures_dir=tmp_path) is None

    def test_dedupe_scoped_to_bssid(self, tmp_path):
        # Same ANonce, different BSSIDs → both must write (different APs).
        ap1 = _ap_with_hs(bssid="aa:bb:cc:dd:ee:ff",
                          anonce=b"\xA0" + b"\x00" * 31)
        ap2 = _ap_with_hs(bssid="11:22:33:44:55:66",
                          anonce=b"\xA0" + b"\x00" * 31)
        p1 = save_handshake(ap1, "11:22:33:44:55:66", captures_dir=tmp_path)
        p2 = save_handshake(ap2, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert p1 is not None and p2 is not None and p1 != p2


# ---- save_pmkid ------------------------------------------------------------

class TestSavePmkid:
    def test_writes_hc22000_and_pcap(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x11" * 16, with_pair=False)
        path = save_pmkid(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert path is not None
        assert path.name.endswith("_pmkid.hc22000")
        assert path.with_suffix(".pcap").exists()

    def test_body_is_wpa01_only(self, tmp_path):
        # AP also has a full 4-way — save_pmkid must NOT include the WPA*02 line.
        ap = _ap_with_hs(pmkid=b"\x22" * 16)  # also has m1/m2
        path = save_pmkid(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "WPA*01*" in text
        assert "WPA*02*" not in text

    def test_dedupes_same_pmkid(self, tmp_path):
        pmkid = b"\x33" * 16
        ap = _ap_with_hs(pmkid=pmkid, with_pair=False)
        first = save_pmkid(ap, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert first is not None
        ap2 = _ap_with_hs(pmkid=pmkid, with_pair=False)
        again = save_pmkid(ap2, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert again is None

    def test_rotated_pmkid_writes_new(self, tmp_path):
        ap1 = _ap_with_hs(pmkid=b"\x44" * 16, with_pair=False)
        ap2 = _ap_with_hs(pmkid=b"\x55" * 16, with_pair=False)
        save_pmkid(ap1, "11:22:33:44:55:66", captures_dir=tmp_path)
        second = save_pmkid(ap2, "11:22:33:44:55:66", captures_dir=tmp_path)
        assert second is not None

    def test_no_pmkid_returns_none(self, tmp_path):
        ap = _ap_with_hs(with_pair=False)
        assert save_pmkid(ap, "11:22:33:44:55:66", captures_dir=tmp_path) is None

    def test_hidden_ssid_returns_none(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x66" * 16, with_pair=False)
        ap.ssid = None
        assert save_pmkid(ap, "11:22:33:44:55:66", captures_dir=tmp_path) is None


# ---- save_wep_key ----------------------------------------------------------

class TestSaveWepKey:
    def test_writes_ascii_when_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = save_wep_key(ap, b"abcde", captures_dir=tmp_path)
        assert path is not None
        assert path.name.endswith("_wep_key.txt")
        body = path.read_text(encoding="utf-8")
        assert "WEP key (hex):   6162636465" in body
        assert 'WEP key (ASCII): "abcde"' in body

    def test_writes_hex_only_when_non_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = save_wep_key(ap, b"\x00\x01\x02\x03\x04", captures_dir=tmp_path)
        body = path.read_text(encoding="utf-8")
        assert "WEP key (hex):   0001020304" in body
        assert "ASCII" not in body

    def test_dedupes_same_key(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wep_key(ap, b"abcde", captures_dir=tmp_path) is not None
        assert save_wep_key(ap, b"abcde", captures_dir=tmp_path) is None

    def test_different_key_writes_new(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wep_key(ap, b"abcde", captures_dir=tmp_path)
        assert save_wep_key(ap, b"fghij", captures_dir=tmp_path) is not None

    def test_hidden_ssid_uses_fallback_filename(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
        path = save_wep_key(ap, b"abcde", captures_dir=tmp_path)
        assert path is not None
        assert path.name.startswith("hidden_")
        body = path.read_text(encoding="utf-8")
        assert "SSID:  <hidden>" in body

    def test_empty_key_returns_none(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wep_key(ap, b"", captures_dir=tmp_path) is None


# ---- save_wps_pin / save_wps_pbc ------------------------------------------

class TestSaveWpsPin:
    def test_writes_with_psk_and_pin(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = save_wps_pin(ap, "12345670", "abcdefgh", captures_dir=tmp_path)
        assert path is not None
        assert path.name.endswith("_wps_pin.txt")
        body = path.read_text(encoding="utf-8")
        assert "SSID: HomeNet" in body
        assert "BSSID: aa:bb:cc:dd:ee:ff" in body
        assert "PSK: abcdefgh" in body
        assert "PIN: 12345670" in body
        assert "method:" not in body

    def test_dedupes_same_pin_and_psk(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wps_pin(ap, "12345670", "abcdefgh", captures_dir=tmp_path) is not None
        assert save_wps_pin(ap, "12345670", "abcdefgh", captures_dir=tmp_path) is None

    def test_psk_rotation_writes_new(self, tmp_path):
        # Same PIN but PSK rotated — high-value: re-verify caught the rotation.
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wps_pin(ap, "12345670", "oldpsk", captures_dir=tmp_path)
        assert save_wps_pin(ap, "12345670", "newpsk", captures_dir=tmp_path) is not None

    def test_empty_inputs_return_none(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wps_pin(ap, "", "psk", captures_dir=tmp_path) is None
        assert save_wps_pin(ap, "12345670", "", captures_dir=tmp_path) is None


class TestSaveWpsPbc:
    def test_writes_psk_only(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = save_wps_pbc(ap, "abcdefgh", captures_dir=tmp_path)
        assert path is not None
        assert path.name.endswith("_wps_pbc.txt")
        body = path.read_text(encoding="utf-8")
        assert "PSK: abcdefgh" in body
        assert "PIN:" not in body
        assert "method:" not in body

    def test_dedupes_same_psk(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wps_pbc(ap, "abcdefgh", captures_dir=tmp_path) is not None
        assert save_wps_pbc(ap, "abcdefgh", captures_dir=tmp_path) is None

    def test_different_psk_writes_new(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wps_pbc(ap, "psk1", captures_dir=tmp_path)
        assert save_wps_pbc(ap, "psk2", captures_dir=tmp_path) is not None


# ---- SSID sanitization (path traversal) ------------------------------------

class TestSsidSanitization:
    def test_traversal_chars_neutered(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="../evil name/")
        path = save_wps_pbc(ap, "psk", captures_dir=tmp_path)
        assert path is not None
        assert path.parent == tmp_path
        assert "/" not in path.name and "\\" not in path.name
        # And the same neutering applies to every kind:
        for fn in (save_wep_key, lambda a, **kw: save_wps_pin(a, "12345670", "psk", **kw)):
            ap2 = AccessPoint(bssid="11:22:33:44:55:66", ssid="../etc/passwd")
            if fn is save_wep_key:
                p = fn(ap2, b"abcde", captures_dir=tmp_path)
            else:
                p = fn(ap2, captures_dir=tmp_path)
            assert p is not None
            assert p.parent == tmp_path
            assert "/" not in p.name and "\\" not in p.name

    def test_long_ssid_truncated(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="A" * 100)
        path = save_wps_pbc(ap, "psk", captures_dir=tmp_path)
        # 32 cap on the ssid portion; the bssid+epoch+suffix follow.
        ssid_part = path.name.split("_aa-bb-cc-dd-ee-ff_")[0]
        assert len(ssid_part) == 32
