"""WPA-PSK key derivation tests: IEEE 802.11i Annex J PMK vectors + MIC self-consistency."""
from wifit3.crack import wpa_psk
from wifit3.dot11.eapol import eapol_key, set_mic, MIC_OFFSET, MIC_LEN


def test_pmk_ieee_annex_j_vectors():
    assert wpa_psk.pmk("password", "IEEE").hex() == (
        "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")
    assert wpa_psk.pmk("ThisIsAPassword", "ThisIsASSID").hex() == (
        "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af")


def test_ptk_is_deterministic_and_nonce_order_independent():
    p = wpa_psk.pmk("password", "IEEE")
    aa, spa = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
    an, sn = bytes(range(32)), bytes(range(32, 64))
    assert wpa_psk.ptk(p, aa, spa, an, sn) == wpa_psk.ptk(p, spa, aa, sn, an)  # sorted pairs
    assert len(wpa_psk.ptk(p, aa, spa, an, sn)) == 48
    assert wpa_psk.kck(wpa_psk.ptk(p, aa, spa, an, sn)) == wpa_psk.ptk(p, aa, spa, an, sn)[:16]


def test_m2_mic_round_trips():
    ssid, psk = "ThisIsASSID", "ThisIsAPassword"
    aa, spa = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
    anonce, snonce = bytes(range(32)), bytes(range(64, 96))
    m2_zeroed = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=snonce,
                          key_data=bytes.fromhex("30140100000fac040100000fac040100000fac020000"))
    mic = wpa_psk.mic_for(psk, ssid, aa, spa, anonce, snonce, m2_zeroed)
    signed = set_mic(m2_zeroed, mic)
    recovered = signed[:MIC_OFFSET] + bytes(MIC_LEN) + signed[MIC_OFFSET + MIC_LEN:]
    assert wpa_psk.mic_for(psk, ssid, aa, spa, anonce, snonce, recovered) == mic
    assert wpa_psk.mic_for("wrongpass", ssid, aa, spa, anonce, snonce, recovered) != mic
