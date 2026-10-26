"""WPA-PSK key derivation tests: IEEE 802.11i Annex J PMK vectors + MIC self-consistency."""
import hashlib
import hmac

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.dot11.eapol import eapol_key, set_mic, MIC_OFFSET, MIC_LEN
from wifit3.dot11.wpa import (
    KCK_LEN,
    derive_pmk,
    derive_ptk,
    derive_ptk_sha256,
    eapol_mic_aes_cmac,
    eapol_mic_hmac_md5,
)


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


_SSID = bytes.fromhex("5468697349734153534944")
_PMK = derive_pmk("ThisIsAPassword", _SSID)
_WRONG_PMK = derive_pmk("ThisIsAPassword!", _SSID)
_AA = bytes.fromhex("112233445566")
_SPA = bytes.fromhex("aabbccddeeff")
_ANONCE = bytes.fromhex("a0" * 32)
_SNONCE = bytes.fromhex("5b" * 32)
_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac060000")


def _eapol_line(mic: bytes, eapol: bytes) -> Hc22000Line:
    return Hc22000Line(kind="02", pmkid_or_mic=mic.hex(), mac_ap=_AA.hex(), mac_sta=_SPA.hex(),
                       essid=_SSID.hex(), anonce=_ANONCE.hex(), eapol=eapol.hex(), message_pair="00")


def test_psk_matches_key_descriptor_version_1():
    eapol = eapol_key(key_info=0x0109, key_len=0, replay=1, nonce=_SNONCE, key_data=_RSN_IE)
    kck = derive_ptk(_PMK, _AA, _SPA, _ANONCE, _SNONCE, KCK_LEN)
    line = _eapol_line(eapol_mic_hmac_md5(kck, eapol), eapol)
    assert wpa_psk.psk_matches(_PMK, line)
    assert not wpa_psk.psk_matches(_WRONG_PMK, line)


def test_psk_matches_key_descriptor_version_3():
    eapol = eapol_key(key_info=0x010B, key_len=0, replay=1, nonce=_SNONCE, key_data=_RSN_IE)
    kck = derive_ptk_sha256(_PMK, _AA, _SPA, _ANONCE, _SNONCE, KCK_LEN)
    line = _eapol_line(eapol_mic_aes_cmac(kck, eapol), eapol)
    assert wpa_psk.psk_matches(_PMK, line)
    assert not wpa_psk.psk_matches(_WRONG_PMK, line)


def test_ptk_sha256_kdf_layout():
    """IEEE 802.11 KDF-SHA256: HMAC-SHA256(PMK, i LE16 | label | sorted MACs | sorted nonces | 384 LE16)."""
    context = _AA + _SPA + _SNONCE + _ANONCE
    block1 = hmac.new(_PMK, bytes.fromhex("0100") + b"Pairwise key expansion" + context
                      + bytes.fromhex("8001"), hashlib.sha256).digest()
    block2 = hmac.new(_PMK, bytes.fromhex("0200") + b"Pairwise key expansion" + context
                      + bytes.fromhex("8001"), hashlib.sha256).digest()
    assert derive_ptk_sha256(_PMK, _AA, _SPA, _ANONCE, _SNONCE, 48) == (block1 + block2)[:48]
    assert derive_ptk_sha256(_PMK, _SPA, _AA, _SNONCE, _ANONCE, KCK_LEN) == block1[:KCK_LEN]


def test_psk_matches_sha256_pmkid_when_message_pair_bit_set():
    digest = hmac.new(_PMK, b"PMK Name" + _AA + _SPA, hashlib.sha256).digest()[:16]
    assert wpa_psk.sha256_pmkid(_PMK, _AA, _SPA) == digest
    for message_pair in ("03", "02"):
        line = Hc22000Line(kind="01", pmkid_or_mic=digest.hex(), mac_ap=_AA.hex(), mac_sta=_SPA.hex(),
                           essid=_SSID.hex(), message_pair=message_pair)
        assert wpa_psk.psk_matches(_PMK, line)
        assert not wpa_psk.psk_matches(_WRONG_PMK, line)
    unflagged = Hc22000Line(kind="01", pmkid_or_mic=digest.hex(), mac_ap=_AA.hex(),
                            mac_sta=_SPA.hex(), essid=_SSID.hex())
    assert not wpa_psk.psk_matches(_PMK, unflagged)
    assert wpa_psk.pmkid(_PMK, _AA, _SPA) != digest


# FT-PSK known answers, generated outside wifit3 from hostapd's sha256_prf_bits / wpa_derive_pmk_r0 /
# wpa_derive_pmk_r1 / wpa_pmk_r1_to_ptk (stdlib hashlib + hmac) with the MIC from `openssl mac CMAC`.
_FT_COMMON = "*020000000001*020000000002*66742d70736b2d74657374"
_FT_IDENTIFIERS = "*a1b2*72306b682e6578616d706c652e74657374*020000000001"
_FT_PMKID_LINE = f"WPA*03*a296df8e01721dbac1685bc39d5b78ad{_FT_COMMON}***{_FT_IDENTIFIERS}"
_FT_EAPOL_LINE = (
    f"WPA*04*9762f3d5bd2953bc2c670a4646b532e7{_FT_COMMON}"
    "*101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
    "*020300fb02010b00100000000000000001404142434445464748494a4b4c4d4e4f505152535455565758595a5b5"
    "c5d5e5f0000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000009c30260100000fac040100000fac040100000fac0400000100a296df8e01721dbac1685bc39d5b7"
    "8ad3603a1b201376d000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000010"
    "6020000000001031172306b682e6578616d706c652e74657374"
    f"*80{_FT_IDENTIFIERS}")


def test_psk_matches_ft_pmkid_known_answer():
    assert _matches("correct horse battery", _FT_PMKID_LINE)
    assert not _matches("correct horse battery!", _FT_PMKID_LINE)


def test_psk_matches_ft_eapol_known_answer():
    assert _matches("correct horse battery", _FT_EAPOL_LINE)
    assert not _matches("correct horse battery!", _FT_EAPOL_LINE)
