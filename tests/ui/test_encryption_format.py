"""Tests for encryption / WEP / AKM-variant markup (format_encryption_markup)."""
from wifit3.models import AccessPoint, WepStats
from wifit3.ui.encryption_format import format_encryption_markup


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
    """Focus's CAPTURE panel owns the IV count — the detailed ENCRYPT line
    must not duplicate it."""
    ap = _ap(encryption="WEP", wep=WepStats(unique_ivs=1234))
    assert format_encryption_markup(ap, detailed=True) == (
        "[bright_green]WEP[/bright_green]"
    )


# ---- AKM-variant visibility in the ENCRYPT detail --------------------------

def test_plain_psk_token_unchanged():
    assert "(PSK)" in format_encryption_markup(_ap(akms=["PSK"]), muted="dim")


def test_psk_sha256_shown_distinctly():
    """PSK-SHA256 (AKM 6) must NOT collapse into 'PSK' — we want to see its
    real-world prevalence in a scan."""
    assert "(PSK256)" in format_encryption_markup(_ap(akms=["PSK-SHA256"]), muted="dim")


def test_ft_psk_shown_distinctly():
    assert "(FT-PSK)" in format_encryption_markup(_ap(akms=["FT-PSK"]), muted="dim")


def test_psk_variants_not_collapsed():
    """A PSK + FT-PSK AP keeps both variants visible, slash-joined."""
    m = format_encryption_markup(_ap(akms=["PSK", "FT-PSK"]), muted="dim")
    assert "(PSK/FT-PSK)" in m
