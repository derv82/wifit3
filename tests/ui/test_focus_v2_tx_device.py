"""TX-device picker end to end inside the real Focus screen: a two-card pool, the peek that shows
the elected TX card, and picking the other card (pin -> re-sync -> art + label swap). Exercises the
overlay opening within the height-capped Focus layout, which the isolated picker test can't. Real
WlanArray + WlanInterface (mock driver), no hardware."""
from textual.app import App

from wifit3.chips.driver import FakeMacSupport
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.art import BreathingArt
from wifit3.ui.screens.focus_v2.card_endpoint import CardEndpoint
from wifit3.ui.screens.focus_v2.tx_picker import TxDevicePicker
from wifit3.wlan.array import WlanArray
from wifit3.wlan.interface import WlanInterface

from tests.frames import pkt


class MockDriver:
    FAKE_MAC = FakeMacSupport.SPOOFABLE
    SUPPORTED_CHANNELS = [1, 6, 11]

    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass


def _member(name, product):
    return WlanInterface(MockDriver(), name, "Mock card", chipset="MT7612U", product_name=product)


def _beacon(bssid, ssid, ch):
    return pkt({"type": "beacon", "bssid": bssid, "ssid": ssid, "source": bssid,
               "dest": "ff:ff:ff:ff:ff:ff", "channel": ch, "rssi": -40,
               "encryption": "WPA2", "akms": ["PSK"], "akm_suites": [2],
               "pairwise_cipher": "CCMP", "raw": b"\xff-beacon-raw"})


class _Host(App):
    def __init__(self, array, ap):
        super().__init__()
        self.array = array
        self.target_ap = ap
        self.pbc_enabled = True

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


def _two_card_focus():
    m0 = _member("wlan0", "Netgear A9000")     # art: card-netgeara9000.ans
    m1 = _member("wlan1", "AWUS036H")     # art: card-awus036h.ans
    array = WlanArray()
    array.attach(m0)
    array.attach(m1)
    bssid = "aa:bb:cc:dd:ee:01"
    m0._on_frame_parsed(_beacon(bssid, "TESTNET", 1))   # feeds the real array sink via _ingest
    return array, array.access_points[bssid], m0, m1


async def test_focus_peeks_elected_tx_card_then_pins_the_chosen_one():
    array, ap, m0, m1 = _two_card_focus()
    async with _Host(array, ap).run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        focus = pilot.app.screen
        card = focus.query_one("#card", CardEndpoint)
        picker = card.query_one(TxDevicePicker)

        # Two cards on ch1: dropdown affordance, and the shown card is the elected TX card (m0,
        # the array's default pick when nothing is pinned).
        assert picker._text.endswith("▼")
        assert picker._current is m0
        assert picker._text.startswith("Netgear A9000")
        assert card.query_one(BreathingArt)._name == "cards/card-netgeara9000.ans"

        # Open the overlay inside the real Focus layout and pin the other card.
        picker.action_open()
        await pilot.pause(0)
        overlay = picker.query_one("#tx-overlay")
        assert overlay.display is True and overlay.option_count == 2

        overlay.highlighted = 1          # members order [m0, m1] -> m1
        await pilot.pause(0)
        await pilot.press("enter")
        await pilot.pause(0)

        # The pin took, and the endpoint re-synced to the pinned card (label + art swap).
        assert array.preferred is m1
        assert picker._current is m1
        assert picker._text.startswith("AWUS036H")
        assert card.query_one(BreathingArt)._name == "cards/card-awus036h.ans"
        assert overlay.display is False
