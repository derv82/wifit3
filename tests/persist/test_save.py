"""Tests for the typed auto-save module (persist.save)."""
from __future__ import annotations

from wifit3.models import AccessPoint, HandshakeMessage, Handshake
from wifit3.persist.save import (
    HcFiles,
    consolidate_hc_files,
    save_handshake,
    save_pmkid,
    save_wep_key,
    save_wps_pbc,
    save_wps_pin,
)


# ---- Fixtures (mirror tests/crack/test_hc22000.py) ------------------------

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
        payload_mic: bytes | None = None) -> HandshakeMessage:
    nonce = nonce if nonce is not None else bytes(range(32))
    mic = mic if mic is not None else b"\xAA" * 16
    payload_mic = payload_mic if payload_mic is not None else mic
    return HandshakeMessage(
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
        hs.messages.extend([m1, m2])
    ap.handshakes[client_mac] = hs
    return ap


# ---- save_handshake --------------------------------------------------------

class TestSaveHandshake:
    def test_writes_hc22000_and_pcap(self, tmp_path):
        ap = _ap_with_hs()
        result = save_handshake(ap, "11:22:33:44:55:66")
        assert result is not None and result.was_new is True
        assert result.path.name == "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"
        assert result.path.exists()
        pcaps = list(tmp_path.glob("*_handshake.pcap"))
        assert len(pcaps) == 1 and pcaps[0].stat().st_size > 0

    def test_body_is_wpa02_only(self, tmp_path):
        # AP also has a PMKID, so save_handshake must NOT include the WPA*01 line.
        ap = _ap_with_hs(pmkid=b"\x11" * 16)
        result = save_handshake(ap, "11:22:33:44:55:66")
        assert result is not None
        text = result.path.read_text(encoding="utf-8")
        assert "WPA*02*" in text
        assert "WPA*01*" not in text

    def test_dedupes_same_anonce_and_returns_existing_path(self, tmp_path):
        ap = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        first = save_handshake(ap, "11:22:33:44:55:66")
        assert first is not None and first.was_new is True
        # Rebuild with the same ANonce: dedupe should report the SAME path.
        ap2 = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        again = save_handshake(ap2, "11:22:33:44:55:66")
        assert again is not None
        assert again.was_new is False
        assert again.path == first.path

    def test_different_anonce_writes_new(self, tmp_path):
        ap1 = _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31)
        ap2 = _ap_with_hs(anonce=b"\xC0" + b"\x00" * 31)
        first = save_handshake(ap1, "11:22:33:44:55:66")
        second = save_handshake(ap2, "11:22:33:44:55:66")
        assert second is not None and second.was_new is True
        assert second.path == first.path
        files = list(tmp_path.glob("*.hc22000"))
        assert len(files) == 1
        lines = second.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_hidden_ssid_returns_none(self, tmp_path):
        ap = _ap_with_hs(ssid="")
        # Pydantic rejects empty string into Optional[str]; set after construction.
        ap.ssid = None
        assert save_handshake(ap, "11:22:33:44:55:66") is None
        assert list(tmp_path.iterdir()) == []

    def test_no_valid_pair_returns_none(self, tmp_path):
        ap = _ap_with_hs(with_pair=False)
        assert save_handshake(ap, "11:22:33:44:55:66") is None

    def test_unknown_client_returns_none(self, tmp_path):
        ap = _ap_with_hs()
        assert save_handshake(ap, "ff:ff:ff:ff:ff:ff") is None

    def test_dedupe_scoped_to_bssid(self, tmp_path):
        # Same ANonce, different BSSIDs → both must write (different APs).
        ap1 = _ap_with_hs(bssid="aa:bb:cc:dd:ee:ff",
                          anonce=b"\xA0" + b"\x00" * 31)
        ap2 = _ap_with_hs(bssid="11:22:33:44:55:66",
                          anonce=b"\xA0" + b"\x00" * 31)
        r1 = save_handshake(ap1, "11:22:33:44:55:66")
        r2 = save_handshake(ap2, "11:22:33:44:55:66")
        assert r1 is not None and r2 is not None
        assert r1.was_new and r2.was_new
        assert r1.path != r2.path


# ---- save_pmkid ------------------------------------------------------------

class TestSavePmkid:
    def test_writes_hc22000_only_no_pcap(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x11" * 16, with_pair=False)
        result = save_pmkid(ap, "11:22:33:44:55:66")
        assert result is not None and result.was_new is True
        assert result.path.name == "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"
        # No pcap companion: nothing consumes a PMKID-in-pcap, and the
        # harvest M1 isn't kept anyway, so the file would be beacon-only.
        assert not list(tmp_path.glob("*.pcap"))

    def test_body_is_wpa01_only(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x22" * 16)  # also has m1/m2
        result = save_pmkid(ap, "11:22:33:44:55:66")
        text = result.path.read_text(encoding="utf-8")
        assert "WPA*01*" in text
        assert "WPA*02*" not in text

    def test_dedupes_same_pmkid_and_returns_existing_path(self, tmp_path):
        pmkid = b"\x33" * 16
        ap = _ap_with_hs(pmkid=pmkid, with_pair=False)
        first = save_pmkid(ap, "11:22:33:44:55:66")
        assert first is not None and first.was_new is True
        ap2 = _ap_with_hs(pmkid=pmkid, with_pair=False)
        again = save_pmkid(ap2, "11:22:33:44:55:66")
        assert again is not None and again.was_new is False
        assert again.path == first.path

    def test_rotated_pmkid_writes_new(self, tmp_path):
        ap1 = _ap_with_hs(pmkid=b"\x44" * 16, with_pair=False)
        ap2 = _ap_with_hs(pmkid=b"\x55" * 16, with_pair=False)
        save_pmkid(ap1, "11:22:33:44:55:66")
        second = save_pmkid(ap2, "11:22:33:44:55:66")
        assert second is not None and second.was_new is True
        lines = second.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert len(list(tmp_path.glob("*.hc22000"))) == 1

    def test_no_pmkid_returns_none(self, tmp_path):
        ap = _ap_with_hs(with_pair=False)
        assert save_pmkid(ap, "11:22:33:44:55:66") is None

    def test_hidden_ssid_returns_none(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x66" * 16, with_pair=False)
        ap.ssid = None
        assert save_pmkid(ap, "11:22:33:44:55:66") is None


# ---- save_wep_key ----------------------------------------------------------

class TestSaveWepKey:
    def test_writes_ascii_when_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        result = save_wep_key(ap, b"abcde")
        assert result is not None and result.was_new is True
        assert result.path.name.endswith("_wep_key.txt")
        body = result.path.read_text(encoding="utf-8")
        assert "WEP key (hex):   6162636465" in body
        assert 'WEP key (ASCII): "abcde"' in body

    def test_writes_hex_only_when_non_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        result = save_wep_key(ap, b"\x00\x01\x02\x03\x04")
        body = result.path.read_text(encoding="utf-8")
        assert "WEP key (hex):   0001020304" in body
        assert "ASCII" not in body

    def test_dedupes_same_key_and_returns_existing_path(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        first = save_wep_key(ap, b"abcde")
        assert first is not None and first.was_new is True
        again = save_wep_key(ap, b"abcde")
        assert again is not None and again.was_new is False
        assert again.path == first.path

    def test_different_key_writes_new(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wep_key(ap, b"abcde")
        second = save_wep_key(ap, b"fghij")
        assert second is not None and second.was_new is True

    def test_hidden_ssid_uses_fallback_filename(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
        result = save_wep_key(ap, b"abcde")
        assert result is not None
        assert result.path.name.startswith("hidden_")
        body = result.path.read_text(encoding="utf-8")
        assert "SSID:  <hidden>" in body

    def test_empty_key_returns_none(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wep_key(ap, b"") is None


# ---- save_wps_pin / save_wps_pbc ------------------------------------------

class TestSaveWpsPin:
    def test_writes_with_psk_and_pin(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        result = save_wps_pin(ap, "12345670", "abcdefgh")
        assert result is not None and result.was_new is True
        assert result.path.name.endswith("_wps_pin.txt")
        body = result.path.read_text(encoding="utf-8")
        assert "SSID: HomeNet" in body
        assert "BSSID: aa:bb:cc:dd:ee:ff" in body
        assert "PSK: abcdefgh" in body
        assert "PIN: 12345670" in body
        assert "method:" not in body

    def test_dedupes_same_pin_and_psk_returns_existing_path(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        first = save_wps_pin(ap, "12345670", "abcdefgh")
        assert first is not None and first.was_new is True
        again = save_wps_pin(ap, "12345670", "abcdefgh")
        assert again is not None and again.was_new is False
        assert again.path == first.path

    def test_psk_rotation_writes_new(self, tmp_path):
        # Same PIN but PSK rotated, high-value: re-verify caught the rotation.
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wps_pin(ap, "12345670", "oldpsk")
        second = save_wps_pin(ap, "12345670", "newpsk")
        assert second is not None and second.was_new is True

    def test_empty_inputs_return_none(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert save_wps_pin(ap, "", "psk") is None
        assert save_wps_pin(ap, "12345670", "") is None


class TestSaveWpsPbc:
    def test_writes_psk_only(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        result = save_wps_pbc(ap, "abcdefgh")
        assert result is not None and result.was_new is True
        assert result.path.name.endswith("_wps_pbc.txt")
        body = result.path.read_text(encoding="utf-8")
        assert "PSK: abcdefgh" in body
        assert "PIN:" not in body
        assert "method:" not in body

    def test_dedupes_same_psk_returns_existing_path(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        first = save_wps_pbc(ap, "abcdefgh")
        assert first is not None and first.was_new is True
        again = save_wps_pbc(ap, "abcdefgh")
        assert again is not None and again.was_new is False
        assert again.path == first.path

    def test_different_psk_writes_new(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        save_wps_pbc(ap, "psk1")
        second = save_wps_pbc(ap, "psk2")
        assert second is not None and second.was_new is True


# ---- SSID sanitization (path traversal) ------------------------------------

class TestSsidSanitization:
    def test_traversal_chars_neutered(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="../evil name/")
        result = save_wps_pbc(ap, "psk")
        assert result is not None
        assert result.path.parent == tmp_path
        assert "/" not in result.path.name and "\\" not in result.path.name

    def test_long_ssid_truncated(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="A" * 100)
        result = save_wps_pbc(ap, "psk")
        # 32 cap on the ssid portion; the bssid+epoch+suffix follow.
        ssid_part = result.path.name.split("_aa-bb-cc-dd-ee-ff_")[0]
        assert len(ssid_part) == 32


# ---- Legacy deduplication --------------------------------------------------

class TestLegacyDedupe:
    def test_legacy_split_file_dedupes_and_prevents_duplicate_captures(self, tmp_path):
        legacy_file = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000000_handshake.hc22000"
        anonce_hex = "a0" * 32
        line = f"WPA*02*00000000000000000000000000000000*aabbccddeeff*112233445566*486f6d654e6574*{anonce_hex}*00*00\n"
        legacy_file.write_text(line, encoding="utf-8")

        ap = _ap_with_hs(anonce=b"\xA0" * 32)
        res = save_handshake(ap, "11:22:33:44:55:66")
        assert res is not None and res.was_new is False
        assert res.path == legacy_file
        assert not (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff.hc22000").exists()



# ---- Aggregated mode combined workflow -------------------------------------

class TestSaveAggregatedCombined:
    def test_handshake_and_pmkid_merge_into_same_file(self, tmp_path):
        ap = _ap_with_hs(pmkid=b"\x11" * 16)
        r_hs = save_handshake(ap, "11:22:33:44:55:66")
        assert r_hs is not None and r_hs.was_new is True
        r_pmk = save_pmkid(ap, "11:22:33:44:55:66")
        assert r_pmk is not None and r_pmk.was_new is True
        assert r_hs.path == r_pmk.path
        assert r_hs.path.name == "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"

        lines = r_hs.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert any(ln.startswith("WPA*02*") for ln in lines)
        assert any(ln.startswith("WPA*01*") for ln in lines)

        # Re-saving same handshake or PMKID should dedupe
        assert save_handshake(ap, "11:22:33:44:55:66").was_new is False
        assert save_pmkid(ap, "11:22:33:44:55:66").was_new is False
        lines_after = r_hs.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines_after) == 2


# ---- Consolidate legacy captures -------------------------------------------

class TestConsolidateHcFiles:
    def test_consolidate_empty_and_nonexistent_dir(self, tmp_path):
        assert consolidate_hc_files(tmp_path / "nonexistent") == (0, 0)
        assert consolidate_hc_files(tmp_path) == (0, 0)

    def test_consolidate_multiple_aps_and_deduplicates(self, tmp_path):
        hs_line1 = "WPA*02*01*aabbccddeeff*112233445566*5465737431*11111111111111111111111111111111**\n"
        hs_line2 = "WPA*02*01*aabbccddeeff*112233445566*5465737431*22222222222222222222222222222222**\n"
        pmk_line1 = "WPA*01*33333333333333333333333333333333*aabbccddeeff*112233445566*5465737431***\n"
        office_hs = "WPA*02*01*112233445566*aabbccddeeff*4f6666696365*44444444444444444444444444444444**\n"

        # AP1 files: 2 handshakes (one duplicate), 1 pmkid
        (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000").write_text(hs_line1, encoding="utf-8")
        (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000002_handshake.hc22000").write_text(hs_line1, encoding="utf-8")  # dupe
        (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000003_handshake.hc22000").write_text(hs_line2, encoding="utf-8")
        (tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000004_pmkid.hc22000").write_text(pmk_line1, encoding="utf-8")

        # AP2 files: 1 handshake
        (tmp_path / "Office_11-22-33-44-55-66_1700000005_handshake.hc22000").write_text(office_hs, encoding="utf-8")

        # Non-legacy files: should stay untouched
        pcap_file = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.pcap"
        pcap_file.write_text("dummy pcap", encoding="utf-8")
        wps_file = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000006_wps_pin.txt"
        wps_file.write_text("PIN: 12345670", encoding="utf-8")
        other_file = tmp_path / "notes.txt"
        other_file.write_text("notes", encoding="utf-8")

        migrated, deleted = consolidate_hc_files(tmp_path)
        assert migrated == 2
        assert deleted == 5

        # Verify AP1 file has 3 unique lines (2 handshakes + 1 pmkid)
        ap1_target = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"
        assert ap1_target.exists()
        ap1_lines = ap1_target.read_text(encoding="utf-8").strip().splitlines()
        assert len(ap1_lines) == 3

        # Verify AP2 file has 1 line
        ap2_target = tmp_path / "Office_11-22-33-44-55-66.hc22000"
        assert ap2_target.exists()
        ap2_lines = ap2_target.read_text(encoding="utf-8").strip().splitlines()
        assert len(ap2_lines) == 1

        # Verify non-legacy files are preserved
        assert pcap_file.exists()
        assert wps_file.exists()
        assert other_file.exists()

    def test_consolidate_appends_to_existing_target(self, tmp_path):
        hs_line1 = "WPA*02*01*aabbccddeeff*112233445566*5465737431*11111111111111111111111111111111**\n"
        hs_line2 = "WPA*02*01*aabbccddeeff*112233445566*5465737431*22222222222222222222222222222222**\n"

        target = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"
        target.write_text(hs_line1, encoding="utf-8")

        legacy = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000002_handshake.hc22000"
        legacy.write_text(hs_line2, encoding="utf-8")

        migrated, deleted = consolidate_hc_files(tmp_path)
        assert migrated == 1
        assert deleted == 1
        assert not legacy.exists()

        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


# ---- HcFiles class ---------------------------------------------------------

class TestHcFiles:
    def test_find_existing_anonce_in_aggregate_and_split(self, tmp_path):
        hc = HcFiles(tmp_path, "HomeNet", "aa:bb:cc:dd:ee:ff")
        assert hc.find_existing_anonce("1111") is None

        # Write to aggregate file
        hc.agg_path.write_text("WPA*02*01*aabbccddeeff*112233445566*5465737431*1111**\n", encoding="utf-8")
        assert hc.find_existing_anonce("1111") == hc.agg_path

        # Write to split file
        split_p = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000"
        split_p.write_text("WPA*02*01*aabbccddeeff*112233445566*5465737431*2222**\n", encoding="utf-8")
        assert hc.find_existing_anonce("2222") == split_p

    def test_find_existing_pmkid_in_aggregate_and_split(self, tmp_path):
        hc = HcFiles(tmp_path, "HomeNet", "aa:bb:cc:dd:ee:ff")
        assert hc.find_existing_pmkid("3333") is None

        # Write to aggregate file
        hc.agg_path.write_text("WPA*01*3333*aabbccddeeff*112233445566*5465737431***\n", encoding="utf-8")
        assert hc.find_existing_pmkid("3333") == hc.agg_path

        # Write to split file
        split_p = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000002_pmkid.hc22000"
        split_p.write_text("WPA*01*4444*aabbccddeeff*112233445566*5465737431***\n", encoding="utf-8")
        assert hc.find_existing_pmkid("4444") == split_p


