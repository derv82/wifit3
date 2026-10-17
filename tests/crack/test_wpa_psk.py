"""WPA-PSK key derivation tests: IEEE 802.11i Annex J PMK vectors + MIC self-consistency."""
from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import parse_hc22000
from wifit3.dot11.eapol import eapol_key, set_mic, MIC_OFFSET, MIC_LEN
from wifit3.dot11.wpa import KCK_LEN, derive_pmk, derive_ptk


def test_pmk_ieee_annex_j_vectors():
    assert derive_pmk("password", b"IEEE").hex() == (
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")
    assert derive_pmk("ThisIsAPassword", b"ThisIsASSID").hex() == (
        "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af")


def test_ptk_is_deterministic_and_nonce_order_independent():
    p = derive_pmk("password", b"IEEE")
    aa, spa = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
    an, sn = bytes(range(32)), bytes(range(32, 64))
    assert derive_ptk(p, aa, spa, an, sn, 48) == derive_ptk(p, spa, aa, sn, an, 48)  # sorted pairs
    assert len(derive_ptk(p, aa, spa, an, sn, 48)) == 48
    assert derive_ptk(p, aa, spa, an, sn, KCK_LEN) == derive_ptk(p, aa, spa, an, sn, 48)[:16]


def test_m2_mic_round_trips():
    ssid, psk = b"ThisIsASSID", "ThisIsAPassword"
    aa, spa = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
    anonce, snonce = bytes(range(32)), bytes(range(64, 96))
    m2_zeroed = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=snonce,
                          key_data=bytes.fromhex("30140100000fac040100000fac040100000fac020000"))
    mic = wpa_psk.mic_for(psk, ssid, aa, spa, anonce, snonce, m2_zeroed)
    signed = set_mic(m2_zeroed, mic)
    recovered = signed[:MIC_OFFSET] + bytes(MIC_LEN) + signed[MIC_OFFSET + MIC_LEN:]
    assert wpa_psk.mic_for(psk, ssid, aa, spa, anonce, snonce, recovered) == mic
    assert wpa_psk.mic_for("wrongpass", ssid, aa, spa, anonce, snonce, recovered) != mic


# hashcat wiki example_hashes, mode 22000 (password "hashcat!").
_HASHCAT_PMKID_LINE = (
    "WPA*01*4d4fe7aac3a2cecab195321ceb99a7d0*fc690c158264*f4747f87f9f4"
    "*686173686361742d6573736964***")
_HASHCAT_EAPOL_LINE = (
    "WPA*02*024022795224bffca545276c3762686f*6466b38ec3fc*225edc49b7aa"
    "*54502d4c494e4b5f484153484341545f54455354"
    "*10e3be3b005a629e89de088d6a2fdc489db83ad4764f2d186b9cde15446e972e"
    "*0103007502010a0000000000000000000148ce2ccba9c1fda130ff2fbbfb4fd3b063d1a93920b0f7df54a5cb"
    "f787b16171000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000001630140100000fac040100000fac040100000fac028000*a2")


def _matches(psk: str, line: str) -> bool:
    parsed = parse_hc22000(line)
    assert parsed is not None
    return wpa_psk.psk_matches(derive_pmk(psk, bytes.fromhex(parsed.essid)), parsed)


def test_psk_matches_hashcat_pmkid_example():
    assert _matches("hashcat!", _HASHCAT_PMKID_LINE)
    assert not _matches("hashcat?", _HASHCAT_PMKID_LINE)


def test_psk_matches_hashcat_eapol_example():
    assert _matches("hashcat!", _HASHCAT_EAPOL_LINE)
    assert not _matches("hashcat?", _HASHCAT_EAPOL_LINE)


def test_psk_matches_rejects_key_descriptor_version_3():
    fields = _HASHCAT_EAPOL_LINE.split("*")
    eapol = bytearray.fromhex(fields[7])
    eapol[6] = (eapol[6] & ~7) | 3
    fields[7] = eapol.hex()
    assert not _matches("hashcat!", "*".join(fields))
