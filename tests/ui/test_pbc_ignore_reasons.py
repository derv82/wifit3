from types import SimpleNamespace

from tests.wlan.mocks import build_ap
from wifit3.persist.ap_state import PasswordStatus
from wifit3.persist.config import Config
from wifit3.ui.app import WifiteApp


def _app(status: PasswordStatus, pbc_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(vault=SimpleNamespace(ap_state=lambda bssid: SimpleNamespace(status=status)),
                           pbc_enabled=pbc_enabled)


def test_no_reasons_for_a_visible_unsilenced_ap_without_a_current_psk(monkeypatch):
    monkeypatch.setattr(Config, "silenced_bssids", [])
    ap = build_ap()
    assert WifiteApp.pbc_ignore_reasons(_app(PasswordStatus.NOT_CRACKED, True), ap) == []


def test_every_reason_is_listed(monkeypatch):
    ap = build_ap()
    ap.ssid = ""
    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid.lower()])
    assert ap.is_hidden
    assert WifiteApp.pbc_ignore_reasons(_app(PasswordStatus.CURRENT, False), ap) == [
        "AP wyciszone", "ukryty SSID", "PSK już aktualny", "PBC wyłączone (naciśnij [bold]w[/bold])"]
