import re
from types import SimpleNamespace

import pytest
from textual.app import App

from wifit3.ui.screens.focus_v2.eviltwin_modal import (
    EvilTwinInputModal, _plus_one, _random_bssid, _CYCLES,
)

_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _modal(single: bool, target) -> EvilTwinInputModal:
    m = object.__new__(EvilTwinInputModal)   # exercise the pure knob helpers without a Textual app
    m._single, m.target = single, target
    return m


def test_single_card_locks_channel_to_target_and_bumps_bssid():
    target = SimpleNamespace(channel=6, bssid="94:83:c4:8c:3f:78")
    m = _modal(True, target)
    assert m._default_channel(None) == 6
    assert m._channel_options(None) == [("6 (target)", 6)]
    assert m._default_bssid() == "94:83:c4:8c:3f:79"


def test_multi_card_keeps_decoy_channel_and_target_bssid():
    target = SimpleNamespace(channel=1, bssid="94:83:c4:8c:3f:78")
    m = _modal(False, target)
    twin = SimpleNamespace(supported_channels=[1, 6, 11])
    assert m._default_channel(twin) == 6                        # CSA decoy off ch 1
    assert m._channel_options(twin) == [("1 (target)", 1), ("6", 6), ("11", 11)]
    assert m._default_bssid() == "94:83:c4:8c:3f:78"


def test_plus_one_bumps_last_nibble():
    assert _plus_one("94:83:c4:8c:3f:78") == "94:83:c4:8c:3f:79"
    assert _plus_one("94:83:c4:8c:3f:7f") == "94:83:c4:8c:3f:70"   # wraps f -> 0


def test_random_bssid_is_locally_administered():
    b = _random_bssid()
    assert _MAC.match(b) and b.startswith("02:")


def test_cycle_table():
    by_label = {label: (period, once) for label, period, once in _CYCLES}
    assert by_label["Never"] == (None, False)
    assert by_label["Once"][1] is True
    assert by_label["30 seconds"] == (30.0, False)


def _card(fake_mac):
    """A card stub whose driver reports the given FAKE_MAC support."""
    return SimpleNamespace(driver=SimpleNamespace(FAKE_MAC=fake_mac),
                           name="card", supported_channels=[1, 6, 11])


@pytest.mark.asyncio
async def test_modal_stays_visible_and_compacts_on_small_screens():
    from wifit3.chips.driver import FakeMacSupport
    from wifit3.dot11.pmf import PmfPosture

    target = SimpleNamespace(
        channel=6, bssid="94:83:c4:8c:3f:78", pmf_posture=PmfPosture.DISABLED)
    card = _card(FakeMacSupport.SPOOFABLE)
    card.mac_address = "02:00:00:00:00:01"
    app = App()
    async with app.run_test(size=(38, 12)) as pilot:
        app.push_screen(EvilTwinInputModal(target, [card]))
        await pilot.pause()

        modal = app.screen
        dialog = modal.query_one("#dialog")
        assert modal.has_class("-narrow") and modal.has_class("-short")
        assert dialog.region.x >= 0 and dialog.region.right <= app.size.width
        assert dialog.region.y >= 0 and dialog.region.bottom <= app.size.height
        assert dialog.virtual_size.height > dialog.size.height

        dialog.scroll_end(animate=False)
        await pilot.pause()
        assert modal.query_one("#button-row").region.bottom <= dialog.content_region.bottom


def test_a_card_that_cannot_spoof_a_mac_cannot_host():
    """mt7601u leaves FAKE_MAC at the ABC default, so it is not a valid host."""
    from wifit3.chips.driver import FakeMacSupport
    from wifit3.ui.screens.focus_v2.eviltwin_modal import _can_host
    assert _can_host(_card(FakeMacSupport.NONE)) is False
    assert _can_host(_card(FakeMacSupport.UNIMPLEMENTED)) is False
    assert _can_host(_card(FakeMacSupport.SPOOFABLE)) is True
    assert _can_host(_card(FakeMacSupport.FIXED_MAC)) is True


def test_the_start_button_refuses_when_no_member_can_host():
    """The modal builds Select([...], allow_blank=False) from the host list, so an
    empty list raised EmptySelectError at compose. A live button that crashes on press
    is worse than one that explains itself, so the guard is part of _start_eviltwin."""
    import inspect

    from wifit3.ui.screens.focus_v2 import screen as focus_screen
    src = inspect.getsource(focus_screen.FocusViewV2._start_eviltwin)
    assert "any(_can_host(m) for m in array.members)" in src
    # The guard must return before the modal is pushed, or it never fires.
    assert src.index("if not any(_can_host") < src.index("push_screen")
