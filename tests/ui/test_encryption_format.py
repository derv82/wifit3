"""Tests for encryption / WEP / AKM-variant markup (format_encryption_markup)."""
from wifit3.models import AccessPoint, WepStats
from wifit3.ui.encryption_format import format_encryption_markup, psk_capture_supported


def _ap(**kw) -> AccessPoint:
    return AccessPoint(bssid="aa:bb:cc:dd:ee:ff", **kw)


# ---- WEP ENCRYPT cell (attackable, carries an IV count) --------------------

def test_wep_is_attackable_with_iv_count():
    ap = _ap(encryption="WEP", wep=WepStats(unique_ivs=1234, total_frames=5000))
    m = format_encryption_markup(ap, detailed=False, muted="dim")
    assert "[bright_green]WEP[/bright_green]" in m
    assert "1.2k IVs" in m


def test_wep_zero_ivs_when_unseen():
    m = format_encryption_markup(_ap(encryption="WEP"), detailed=False, muted="dim")
    assert "·0 IVs" in m


def test_wep_detailed_omits_iv_count():
    """Focus's CAPTURE panel owns the IV count: the detailed ENCRYPT line
    must not duplicate it."""
    ap = _ap(encryption="WEP", wep=WepStats(unique_ivs=1234))
    assert format_encryption_markup(ap, detailed=True) == (
        "[bright_green]WEP[/bright_green]"
    )


def test_wpa1_renders_its_parsed_akm_and_cipher():
    ap = _ap(encryption="WPA-EAP-TKIP", akms=["EAP"], akm_suites=[1], pairwise_cipher="TKIP")
    assert format_encryption_markup(ap, detailed=True) == "[red]WPA[/red] [dim](EAP·TKIP)[/dim]"
    assert format_encryption_markup(ap) == "[red]WPA[/red] [dim](EAP)[/dim]"


def test_wpa1_without_parsed_akms_keeps_the_psk_tkip_default():
    ap = _ap(encryption="WPA-PSK-TKIP")
    assert format_encryption_markup(ap, detailed=True) == "[red]WPA[/red] [dim](PSK·TKIP)[/dim]"


# ---- AKM-variant visibility in the ENCRYPT detail --------------------------

def test_plain_psk_token_unchanged():
    assert "(PSK)" in format_encryption_markup(_ap(akms=["PSK"]), muted="dim")


def test_psk_sha256_shown_distinctly():
    """PSK-SHA256 (AKM 6) must NOT collapse into 'PSK': we want to see its
    real-world prevalence in a scan."""
    assert "(PSK256)" in format_encryption_markup(_ap(akms=["PSK-SHA256"]), muted="dim")


def test_ft_psk_shown_distinctly():
    assert "(FT-PSK)" in format_encryption_markup(_ap(akms=["FT-PSK"]), muted="dim")


def test_psk_variants_not_collapsed():
    """A PSK + FT-PSK AP keeps both variants visible, slash-joined."""
    m = format_encryption_markup(_ap(akms=["PSK", "FT-PSK"]), muted="dim")
    assert "(PSK/FT-PSK)" in m


def test_enterprise_akm_variants_not_collapsed():
    variants = {
        "EAP": "EAP",
        "EAP-SHA256": "EAP256",
        "EAP-SUITE-B": "SUITE-B",
        "EAP-SUITE-B-192": "SUITE-B-192",
        "FT-EAP-SUITE-B-192": "FT-SUITE-B-192",
        "FT-EAP-SHA384": "FT-EAP384",
    }
    for akm, token in variants.items():
        assert f"({token})" in format_encryption_markup(_ap(akms=[akm]), muted="dim")


def test_unmapped_known_akms_remain_visible_beside_compact_tokens():
    m = format_encryption_markup(
        _ap(akms=["EAP-SHA256", "EAP-SHA384", "FILS-SHA256"]), muted="dim"
    )
    assert "(EAP256/EAP-SHA384/FILS-SHA256)" in m


def test_wpa2_psk_stays_attackable_green():
    m = format_encryption_markup(_ap(akms=["PSK"], akm_suites=[2]), muted="dim")
    assert "[bright_green]WPA2[/bright_green]" in m


def test_wpa2_enterprise_is_not_painted_attackable():
    m = format_encryption_markup(_ap(akms=["EAP"], akm_suites=[1]), muted="dim")
    assert "[yellow]WPA2[/yellow]" in m
    assert "bright_green" not in m
    assert "(EAP)" in m


def test_wpa3_enterprise_is_not_painted_like_sae():
    m = format_encryption_markup(
        _ap(akms=["EAP-SHA256"], akm_suites=[5], wpa3=True), muted="dim",
    )
    assert "[yellow]WPA3[/yellow]" in m
    assert "bright_red" not in m
    assert "(EAP256)" in m


def test_wpa3_sae_stays_out_of_scope_red():
    m = format_encryption_markup(_ap(akms=["SAE"], akm_suites=[8], wpa3=True), muted="dim")
    assert "[bright_red]WPA3[/bright_red]" in m


def test_wpa3_personal_transition_keeps_attackable_wpa2_half():
    m = format_encryption_markup(
        _ap(akms=["PSK", "SAE"], akm_suites=[2, 8], wpa3=True, transition_mode=True),
        muted="dim",
    )
    assert "[bright_red]WPA3[/bright_red]→[bright_green]2[/bright_green]" in m


def test_wpa3_enterprise_transition_is_not_attackable():
    m = format_encryption_markup(
        _ap(akms=["EAP", "EAP-SHA256"], akm_suites=[1, 5], wpa3=True, transition_mode=True),
        muted="dim",
    )
    assert "[yellow]WPA3[/yellow]→[yellow]2[/yellow]" in m
    assert "bright_green" not in m


def test_mixed_psk_and_eap_stays_attackable():
    m = format_encryption_markup(
        _ap(akms=["PSK", "EAP"], akm_suites=[2, 1]), muted="dim",
    )
    assert "[bright_green]WPA2[/bright_green]" in m
    assert "(PSK/EAP)" in m


def test_wpa2_psk_sha384_only_is_not_painted_attackable():
    """PSK-SHA384 advertises a PSK, but -m 22000 has no crack path for it, so the
    ENCRYPT cell must not promise an attack."""
    m = format_encryption_markup(_ap(akms=["PSK-SHA384"], akm_suites=[0x14]), muted="dim")
    assert "[yellow]WPA2[/yellow]" in m
    assert "bright_green" not in m


def test_wpa2_ft_psk_sha384_only_is_not_painted_attackable():
    m = format_encryption_markup(_ap(akms=["FT-PSK-SHA384"], akm_suites=[0x13]), muted="dim")
    assert "[yellow]WPA2[/yellow]" in m
    assert "bright_green" not in m


def test_wpa2_ft_psk_only_is_painted_attackable():
    """FT-PSK (AKM 4) now has a -m 22000 WPA*04 crack path (upstream 302d0389), so the
    ENCRYPT cell reads attackable; only FT-PSK-SHA384 stays out."""
    m = format_encryption_markup(_ap(akms=["FT-PSK"], akm_suites=[4]), muted="dim")
    assert "[bright_green]WPA2[/bright_green]" in m


def test_fils_enterprise_is_not_painted_attackable():
    m = format_encryption_markup(_ap(akms=["FILS-SHA256"], akm_suites=[14]), muted="dim")
    assert "[yellow]WPA2[/yellow]" in m
    assert "bright_green" not in m


def test_vendor_only_akm_is_known_unsupported():
    ap = _ap(
        encryption="WPA2-AKM-00112299-CCMP",
        akms=["AKM-00112299"],
        akm_suites=[],
        vendor_akm_offered=True,
        rsn_valid=True,
    )
    assert psk_capture_supported(ap) is False
