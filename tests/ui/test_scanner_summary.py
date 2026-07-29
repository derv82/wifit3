"""Scanner pool summary line: names each card + its band tag, pluralizes, None when empty."""
from types import SimpleNamespace

from wifit3.ui.screens.scanner import device_scan_summary


def _member(chipset, channels):
    return SimpleNamespace(chipset=chipset, supported_channels=channels)


def test_none_when_no_members():
    assert device_scan_summary([]) is None


def test_single_device_is_singular():
    line = device_scan_summary([_member("MT7921AU", [1, 6, 36, 149])])
    assert line == ("Scanning with [bold cyan]1[/] device: "
                    "[bold]MT7921AU[/] ([bold cyan]2[/]+[bold green]5G[/])")


def test_multiple_devices_join_with_band_tags():
    members = [
        _member("MT7921AU", [1, 6, 36, 149]),
        _member("AR9271", [1, 6, 11]),
    ]
    line = device_scan_summary(members)
    assert line == ("Scanning with [bold cyan]2[/] devices: "
                    "[bold]MT7921AU[/] ([bold cyan]2[/]+[bold green]5G[/]), "
                    "[bold]AR9271[/] ([bold cyan]2G[/])")
