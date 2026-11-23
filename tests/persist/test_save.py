"""Tests for the typed auto-save module (persist.save)."""
from __future__ import annotations

from wifit3.crack.hc22000_format import read_hc22000
from wifit3.models import AccessPoint, HandshakeMessage, Handshake
from wifit3.persist.save import (
    HcFiles,
    consolidate_hc_files,
    save_handshake,
    save_pmkid,
    write_wep_key,
    write_wpa_psk,
    write_wps_pbc,
    write_wps_pin,
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
    def test_writes_hc22000_only(self, tmp_path):
        ap = _ap_with_hs()
        result = save_handshake(ap, "11:22:33:44:55:66")
        assert result is not None and result.was_new is True
        assert result.path.name == "HomeNet_aa-bb-cc-dd-ee-ff.hc22000"
        assert result.path.exists()
        assert list(tmp_path.glob("*_handshake.pcap")) == []

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

    def test_an_m3_after_a_saved_m1_m2_saves_the_authorized_pair(self, tmp_path):
        anonce = b"\xA0" + b"\x00" * 31
        ap = _ap_with_hs(anonce=anonce)
        assert save_handshake(ap, "11:22:33:44:55:66").was_new is True
        ap.handshakes["11:22:33:44:55:66"].messages.append(_ef(3, replay=6, nonce=anonce, key_data_len=22))

        again = save_handshake(ap, "11:22:33:44:55:66")

        assert again is not None and again.was_new is True
        lines = [line for _, line in read_hc22000(again.path)]
        assert [line.authorized for line in lines] == [False, True]

    def test_an_m1_m2_for_an_anonce_saved_authorized_is_deduped(self, tmp_path):
        anonce = b"\xA0" + b"\x00" * 31
        ap = _ap_with_hs(anonce=anonce)
        ap.handshakes["11:22:33:44:55:66"].messages.append(_ef(3, replay=6, nonce=anonce, key_data_len=22))
        assert save_handshake(ap, "11:22:33:44:55:66").was_new is True

        again = save_handshake(_ap_with_hs(anonce=anonce), "11:22:33:44:55:66")

        assert again is not None and again.was_new is False

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


# ---- write_wep_key ---------------------------------------------------------

class TestWriteWepKey:
    def test_writes_ascii_when_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = write_wep_key(ap, b"abcde")
        assert path.name.endswith("_wep_key.txt")
        body = path.read_text(encoding="utf-8")
        assert "WEP key (hex):   6162636465" in body
        assert 'WEP key (ASCII): "abcde"' in body

    def test_writes_hex_only_when_non_printable(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        body = write_wep_key(ap, b"\x00\x01\x02\x03\x04").read_text(encoding="utf-8")
        assert "WEP key (hex):   0001020304" in body
        assert "ASCII" not in body

    def test_same_key_twice_writes_two_files(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert write_wep_key(ap, b"abcde") != write_wep_key(ap, b"abcde")

    def test_hidden_ssid_uses_fallback_filename(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid=None)
        path = write_wep_key(ap, b"abcde")
        assert path.name.startswith("hidden_")
        assert "SSID:  <hidden>" in path.read_text(encoding="utf-8")


# ---- write_wps_pin / write_wps_pbc ------------------------------------------

class TestWriteWpsPin:
    def test_writes_with_psk_and_pin(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = write_wps_pin(ap, "12345670", "abcdefgh")
        assert path.name.endswith("_wps_pin.txt")
        body = path.read_text(encoding="utf-8")
        assert "SSID: HomeNet" in body
        assert "BSSID: aa:bb:cc:dd:ee:ff" in body
        assert "PSK: abcdefgh" in body
        assert "PIN: 12345670" in body
        assert "method:" not in body

    def test_pin_only_writes_no_psk_line(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        body = write_wps_pin(ap, "12345670", None).read_text(encoding="utf-8")
        assert "PSK" not in body and "PIN: 12345670" in body

    def test_empty_pin_is_written(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        assert "PIN: \n" in write_wps_pin(ap, "", "psk").read_text(encoding="utf-8")


class TestWriteWpsPbc:
    def test_writes_psk_only(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
        path = write_wps_pbc(ap, "abcdefgh")
        assert path.name.endswith("_wps_pbc.txt")
        body = path.read_text(encoding="utf-8")
        assert "PSK: abcdefgh" in body
        assert "PIN:" not in body
        assert "method:" not in body


# ---- write_wpa_psk ----------------------------------------------------------

class TestWriteWpaPsk:
    def test_writes_ssid_bssid_and_psk(self, tmp_path):
        path = write_wpa_psk("Cafe WiFi", "aa:bb:cc:dd:ee:ff", "hashcat!")
        assert path.name.endswith("_wpa_psk.txt")
        assert path.read_text(encoding="utf-8") == (
            "SSID: Cafe WiFi\nBSSID: aa:bb:cc:dd:ee:ff\nPSK: hashcat!\n")


# ---- SSID sanitization (path traversal) ------------------------------------

class TestSsidSanitization:
    def test_traversal_chars_neutered(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="../evil name/")
        path = write_wps_pbc(ap, "psk")
        assert path.parent == tmp_path
        assert "/" not in path.name and "\\" not in path.name

    def test_long_ssid_truncated(self, tmp_path):
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="A" * 100)
        path = write_wps_pbc(ap, "psk")
        # 32 cap on the ssid portion; the bssid+epoch+suffix follow.
        ssid_part = path.name.split("_aa-bb-cc-dd-ee-ff_")[0]
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
        hs_line1 = f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{'11' * 32}**00\n"
        hs_line2 = f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{'22' * 32}**00\n"
        pmk_line1 = f"WPA*01*{'33' * 16}*aabbccddeeff*112233445566*5465737431***\n"
        office_hs = f"WPA*02*{'01' * 16}*112233445566*aabbccddeeff*4f6666696365*{'44' * 32}**00\n"

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
        hs_line1 = f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{'11' * 32}**00\n"
        hs_line2 = f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{'22' * 32}**00\n"

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
        anonce1 = "11" * 32
        anonce2 = "22" * 32
        assert hc.find_existing_anonce(anonce1, False) is None

        # Write to aggregate file
        hc.agg_path.write_text(f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{anonce1}**00\n", encoding="utf-8")
        assert hc.find_existing_anonce(anonce1, False) == hc.agg_path

        # Write to split file
        split_p = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000001_handshake.hc22000"
        split_p.write_text(f"WPA*02*{'01' * 16}*aabbccddeeff*112233445566*5465737431*{anonce2}**00\n", encoding="utf-8")
        assert hc.find_existing_anonce(anonce2, False) == split_p

    def test_find_existing_pmkid_in_aggregate_and_split(self, tmp_path):
        hc = HcFiles(tmp_path, "HomeNet", "aa:bb:cc:dd:ee:ff")
        pmkid1 = "33" * 16
        pmkid2 = "44" * 16
        assert hc.find_existing_pmkid(pmkid1) is None

        # Write to aggregate file
        hc.agg_path.write_text(f"WPA*01*{pmkid1}*aabbccddeeff*112233445566*5465737431***\n", encoding="utf-8")
        assert hc.find_existing_pmkid(pmkid1) == hc.agg_path

        # Write to split file
        split_p = tmp_path / "HomeNet_aa-bb-cc-dd-ee-ff_1700000002_pmkid.hc22000"
        split_p.write_text(f"WPA*01*{pmkid2}*aabbccddeeff*112233445566*5465737431***\n", encoding="utf-8")
        assert hc.find_existing_pmkid(pmkid2) == split_p
