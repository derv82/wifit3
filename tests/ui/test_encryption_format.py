"""Tests for the WPS SECURITY-panel markup (format_wps_markup).

Color contract: green (bright_green) = actionable for us (PIN / Unlocked /
live PBC), dim = FYI capability or anything on a locked AP, red = Locked
dead-end. Version is neutral (uncolored) since it doesn't predict
attackability.
"""
from wifit3.engine.models import AccessPoint
from wifit3.ui.encryption_format import format_wps_markup

# WPS Config Methods bits.
_CM_DISPLAY = 0x0008
_CM_PBC = 0x0080
_CM_KEYPAD = 0x0100
_CM_NFC = 0x0010
_PWID_PBC = 0x0004


def _ap(**kw) -> AccessPoint:
    return AccessPoint(bssid="aa:bb:cc:dd:ee:ff", **kw)


def test_no_wps_hidden():
    assert format_wps_markup(_ap()) is None


def test_unlocked_pin_pbc_exact():
    m = format_wps_markup(_ap(
        wps=True, wps_version="2.0",
        wps_config_methods=_CM_DISPLAY | _CM_PBC | _CM_KEYPAD,
    ))
    assert m == (
        "[bright_green]Unlocked[/bright_green] · WPS2.0 "
        "([bright_green]PIN[/bright_green]·[dim]PushButton[/dim])"
    )


def test_locked_greys_everything():
    m = format_wps_markup(_ap(
        wps=True, wps_locked=True, wps_version="2.0",
        wps_config_methods=_CM_DISPLAY | _CM_PBC,
    ))
    assert m.startswith("\U0001f512 [red]Locked[/red] · [dim]")
    assert m.endswith("WPS2.0 · PIN·PushButton[/dim]")
    # Nothing actionable should render green on a locked AP.
    assert "bright_green" not in m


def test_legacy_pin_only():
    m = format_wps_markup(_ap(wps=True, wps_version="1.0",
                              wps_config_methods=_CM_DISPLAY))
    assert m == (
        "[bright_green]Unlocked[/bright_green] · WPS1.0 "
        "([bright_green]PIN[/bright_green])"
    )


def test_pbc_only_has_no_pin():
    m = format_wps_markup(_ap(wps=True, wps_version="2.0",
                              wps_config_methods=_CM_PBC))
    assert "PIN" not in m
    assert "[dim]PushButton[/dim]" in m


def test_nfc_is_fyi_dim():
    m = format_wps_markup(_ap(wps=True, wps_version="2.0",
                              wps_config_methods=_CM_NFC))
    assert "[dim]NFC[/dim]" in m


def test_pbc_active_appended_when_unlocked():
    m = format_wps_markup(_ap(
        wps=True, wps_version="2.0",
        wps_config_methods=_CM_DISPLAY | _CM_PBC,
        wps_device_password_id=_PWID_PBC,
    ))
    assert m.endswith("· [bright_green]PBC active[/bright_green]")


def test_missing_version_renders_plain_wps():
    m = format_wps_markup(_ap(wps=True, wps_config_methods=_CM_DISPLAY))
    assert " · WPS (" in m   # bare "WPS", no version suffix


def test_version_is_never_colored():
    m = format_wps_markup(_ap(wps=True, wps_version="1.0",
                              wps_config_methods=_CM_DISPLAY))
    assert "WPS1.0" in m
    assert "[yellow]WPS1.0" not in m
    assert "[bright_green]WPS1.0" not in m
