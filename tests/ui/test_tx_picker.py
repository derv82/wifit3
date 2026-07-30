"""TX-device picker: the pure row-state logic (disabled / warn / current) and the mounted widget's
open + select + pin flow. No hardware; ifaces are SimpleNamespaces exposing only what the picker
reads (driver.FAKE_MAC, product_name/chipset, supported_channels)."""
from types import SimpleNamespace

from textual.app import App

from wifit3.chips.driver import FakeMacSupport
from wifit3.ui.screens.focus_v2.tx_picker import TxDevicePicker, build_rows


def _iface(name, channels, fake_mac=FakeMacSupport.SPOOFABLE):
    return SimpleNamespace(driver=SimpleNamespace(FAKE_MAC=fake_mac, product_name=name),
                           product_name=name, chipset=name, supported_channels=list(channels))


def _row_for(rows, iface):
    return next(r for r in rows if r.iface is iface)


# --- build_rows: disabled / warn / current ----------------------------------

def test_build_rows_disables_cards_that_cant_reach_the_band():
    two4 = _iface("ALFA AWUS036NHA", [1, 6, 11])
    five = _iface("Netgear A9000", [36, 44])
    rows = build_rows([two4, five], 44, five)          # target on ch44 (5 GHz)
    assert _row_for(rows, two4).disabled and not _row_for(rows, five).disabled
    assert "2.4 GHz" in _row_for(rows, two4).prompt.plain    # band tag says why it's out
    assert _row_for(rows, five).current and "✓" in _row_for(rows, five).prompt.plain


def test_build_rows_warns_a_less_capable_peer():
    strong = _iface("ALFA AWUS036ACM", [1, 6, 11], FakeMacSupport.SPOOFABLE)
    weak = _iface("AR9271", [1, 6, 11], FakeMacSupport.NONE)
    rows = build_rows([strong, weak], 6, strong)
    assert _row_for(rows, strong).current and not _row_for(rows, strong).disabled
    assert _row_for(rows, weak).prompt.plain.startswith("! ")    # a SPOOFABLE peer exists
    assert not _row_for(rows, strong).prompt.plain.startswith("! ")


def test_build_rows_a_disabled_card_is_never_warned():
    # weak AND out-of-band: disabled wins, no ⚠ (the band mismatch is the salient reason).
    strong = _iface("ALFA AWUS036ACM", [36, 44], FakeMacSupport.SPOOFABLE)
    weak2g = _iface("AR9271", [1, 6, 11], FakeMacSupport.NONE)
    rows = build_rows([strong, weak2g], 44, strong)
    weak_row = next(r for r in rows if r.iface is weak2g)
    assert weak_row.disabled and not weak_row.prompt.plain.startswith("! ")


def test_build_rows_no_target_disables_nothing():
    a = _iface("ALFA AWUS036ACM", [1, 6, 11])
    b = _iface("Netgear A9000", [36, 44])
    rows = build_rows([a, b], None, a)
    assert not any(r.disabled for r in rows)


# --- mounted widget: single vs multi, open + select -------------------------

async def test_picker_single_card_is_a_plain_label():
    solo = _iface("ALFA AWUS036ACM", [1, 6, 11])

    class _Host(App):
        def compose(self):
            yield TxDevicePicker(id="p")

    async with _Host().run_test(size=(80, 24)) as pilot:
        p = pilot.app.query_one(TxDevicePicker)
        p.sync([solo], 6, solo, locked=False)
        await pilot.pause()
        assert p._text == "ALFA AWUS036ACM"     # no caret
        assert p.can_focus is False             # nothing to open


async def test_picker_opens_and_pins_the_chosen_card():
    a = _iface("ALFA AWUS036ACM", [1, 6, 11], FakeMacSupport.SPOOFABLE)
    b = _iface("AR9271", [1, 6, 11], FakeMacSupport.NONE)
    picked = []

    class _Host(App):
        def compose(self):
            yield TxDevicePicker(id="p")

        def on_tx_device_picker_selected(self, event):
            picked.append(event.iface)

    async with _Host().run_test(size=(80, 24)) as pilot:
        p = pilot.app.query_one(TxDevicePicker)
        p.sync([a, b], 6, a, locked=False)
        await pilot.pause()
        assert p._text.endswith("▼") and p.can_focus is True

        p.action_open()
        await pilot.pause()
        ol = p.query_one("#tx-overlay")
        assert ol.display is True and ol.option_count == 2

        ol.highlighted = 1                       # move off the current (a) onto b
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert picked == [b]
        assert p.query_one("#tx-overlay").display is False   # closed after select


async def test_picker_locked_during_campaign_wont_open():
    a = _iface("ALFA AWUS036ACM", [1, 6, 11])
    b = _iface("AR9271", [1, 6, 11])

    class _Host(App):
        def compose(self):
            yield TxDevicePicker(id="p")

    async with _Host().run_test(size=(80, 24)) as pilot:
        p = pilot.app.query_one(TxDevicePicker)
        p.sync([a, b], 6, a, locked=True)
        await pilot.pause()
        assert p.can_focus is False and not p._text.endswith("▼")
        p.action_open()
        await pilot.pause()
        assert p.query_one("#tx-overlay").display is False
