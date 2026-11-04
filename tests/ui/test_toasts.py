from wifit3.ui.toasts import Toasts
from wifit3.watchers.ap_watcher import (
    HandshakeCaptured, PbcWindowOpened, PmkidCaptured, WepKeyRecovered, WpsPbcPskRecovered, WpsPinRecovered,
    WpsPskRecovered,
)

_BSSID = "aa:bb:cc:dd:ee:ff"


class _NotifyRecorder:
    def __init__(self) -> None:
        self.toasts: list[tuple[str, str]] = []

    def notify(self, message: str, *, title: str, timeout: float) -> None:
        self.toasts.append((title, message))


def _handshake(client_mac: str, pair_label: str, ssid="Net") -> HandshakeCaptured:
    return HandshakeCaptured(bssid=_BSSID, ssid=ssid, channel=6, client_mac=client_mac,
                             pair_label=pair_label, authorized=pair_label != "M1+M2")


def _titles(app: _NotifyRecorder) -> list[str]:
    return [title for title, _ in app.toasts]


def test_a_capture_toasts_only_when_it_outranks_the_one_shown_for_the_ap():
    app = _NotifyRecorder()
    toasts = Toasts(app)
    toasts.ap_event(_handshake("11:11:11:11:11:11", "M1+M2"), "", [])
    toasts.ap_event(_handshake("22:22:22:22:22:22", "M1+M2"), "", [])    # another client, same rank
    toasts.ap_event(_handshake("11:11:11:11:11:11", "M2+M3"), "", [])
    toasts.ap_event(PmkidCaptured(bssid=_BSSID, ssid="Net", channel=6,
                                  client_mac="11:11:11:11:11:11"), "", [])
    assert _titles(app) == ["Przechwycono handshake (M1+M2)", "Przechwycono handshake (M2+M3)"]


def test_a_hidden_ap_is_named_by_its_guessid():
    app = _NotifyRecorder()
    Toasts(app).ap_event(_handshake("11:11:11:11:11:11", "M1+M2", ssid=None), "MyRouter", [])
    Toasts(app).ap_event(_handshake("11:11:11:11:11:11", "M1+M2", ssid=None), "", [])
    assert f"{_BSSID}[/bold] ([italic]MyRouter?[/italic])" in app.toasts[0][1]
    assert f"{_BSSID}[/bold] ([italic]<Ukryta>[/italic])" in app.toasts[1][1]


def test_recovered_keys_toast_every_time():
    app = _NotifyRecorder()
    toasts = Toasts(app)
    wep = WepKeyRecovered(bssid=_BSSID, ssid="Net", channel=6, key_hex="6162636465")
    toasts.ap_event(wep, "", [])
    toasts.ap_event(wep, "", [])
    toasts.ap_event(WpsPinRecovered(bssid=_BSSID, ssid="Net", channel=6, pin="12345670"), "", [])
    toasts.ap_event(WpsPskRecovered(bssid=_BSSID, ssid="Net", channel=6, psk="hunter2"), "", [])
    toasts.ap_event(WpsPbcPskRecovered(bssid=_BSSID, ssid="Net", channel=6, psk="latte123"), "", [])
    assert _titles(app) == ["Odzyskano klucz WEP", "Odzyskano klucz WEP", "Odzyskano WPS PIN",
                            "Odzyskano WPS PSK",
                            "Odzyskano WPS PSK (PushButton)"]


def test_a_pbc_window_toasts_every_reason_it_is_ignored_or_that_it_is_targeted():
    app = _NotifyRecorder()
    toasts = Toasts(app)
    window = PbcWindowOpened(bssid=_BSSID, ssid="Net", channel=6)
    toasts.ap_event(window, "", ["AP silenced", "PSK already current"])
    toasts.ap_event(window, "", [])
    assert _titles(app) == ["Okno WPS PBC otwarte", "Okno WPS PBC otwarte"]
    assert app.toasts[0][1].endswith("[dim]pominięto: AP silenced, PSK already current[/dim]")
    assert app.toasts[1][1].endswith("Namierzam")
