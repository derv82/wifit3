"""Card-art selection: which .ans a pooled card shows, the label under it, the
OUI -> product_name refinement that disambiguates same-VID:PID variants, and the
BreathingArt runtime art swap. Pure logic, no mounting (BreathingArt is driven by
hand, as in test_focus_v2_layout)."""
from types import SimpleNamespace

from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.chips.mt7921au.driver import MT7921AUDriver
from wifit3.chips.products import AMBIGUOUS_AR9271, AMBIGUOUS_MT7921AU, ALFA, Panda, TPLink
from wifit3.ui.screens.focus_v2 import art


def _iface(product_name=None, chipset=None, driver_product=None):
    """A stand-in for WlanInterface: only the fields art selection reads."""
    return SimpleNamespace(driver=SimpleNamespace(product_name=driver_product),
                           product_name=product_name, chipset=chipset)


# --- OUI -> product_name (driver-side) --------------------------------------

def test_derive_product_name_by_oui():
    # ALFA 00:c0:ca, Panda 9c:ef:d5 (the two makes that share the MT7921AU VID:PID).
    assert MT7921AUDriver.derive_product_name("00:c0:ca:ba:4e:91") == ALFA.AWUS036AXML
    assert MT7921AUDriver.derive_product_name("9c:ef:d5:f6:44:a4") == Panda.PAU0F


def test_derive_product_name_case_insensitive():
    assert MT7921AUDriver.derive_product_name("00:C0:CA:BA:4E:91") == ALFA.AWUS036AXML


def test_derive_product_name_unknown_or_missing():
    assert MT7921AUDriver.derive_product_name("de:ad:be:ef:00:01") is None
    assert MT7921AUDriver.derive_product_name(None) is None
    assert MT7921AUDriver.derive_product_name("") is None


def test_derive_product_name_ar9271_by_oui():
    # 0cf3:9271 is shared: a TP-Link OUI is the WN722N v1, any other real MAC defaults to the ALFA.
    assert AR9271V2Driver.derive_product_name("f4:ec:38:aa:bb:cc") == TPLink.TL_WN722N_V1
    assert AR9271V2Driver.derive_product_name("F4:EC:38:AA:BB:CC") == TPLink.TL_WN722N_V1
    assert AR9271V2Driver.derive_product_name("00:c0:ca:aa:bb:cc") == ALFA.AWUS036NHA   # ALFA OUI
    assert AR9271V2Driver.derive_product_name("de:ad:be:ef:00:01") == ALFA.AWUS036NHA   # unknown -> ALFA
    assert AR9271V2Driver.derive_product_name(None) is None                               # MAC-less -> combined
    assert AR9271V2Driver.derive_product_name("") is None


# --- art_path_for: product -> chipset -> generic ----------------------------

def test_art_path_for_product_hit():
    assert art.art_path_for(_iface(product_name="Panda PAU05/06")) == "cards/card-pau06.ans"


def test_every_mapped_art_file_exists():
    for tier, mapping in (("product", art._ART_BY_PRODUCT), ("chipset", art._ART_BY_CHIPSET)):
        for key, path in mapping.items():
            assert art._exists(path), f"{tier} {key!r} -> {path!r} is missing on disk"


def test_art_path_for_wn722n_v1_shares_v23_art():
    assert art.art_path_for(_iface(product_name="TL-WN722N v1")) == "cards/card-tpwn722nv23.ans"


def test_art_path_for_ar9271_combined_label_defaults_to_alfa():
    # Pre-connect / MAC-less: the unsplit label resolves to the ALFA art, not the generic fallback.
    assert art.art_path_for(_iface(product_name=AMBIGUOUS_AR9271)) == "cards/card-awus036nha.ans"


def test_art_path_for_driver_refined_name_wins():
    # Static name is the ambiguous shared label; the driver's OUI-refined name takes precedence.
    iface = _iface(product_name=AMBIGUOUS_MT7921AU, driver_product=Panda.PAU0F)
    assert art.art_path_for(iface) == "cards/card-pau0f.ans"


def test_art_path_for_chipset_fallback():
    # A row with no product_name (e.g. RT5370 / LOTEKOO) resolves via the chipset tier.
    assert art.art_path_for(_iface(chipset="RT5370")) == "cards/card-lotekoo150.ans"


def test_art_path_for_generic_fallback():
    assert art.art_path_for(_iface(product_name="Nonesuch", chipset="NOPE")) == art._GENERIC


def test_card_art_skips_missing_file(monkeypatch):
    # A map entry pointing at a not-yet-drawn .ans is skipped, not crashed on.
    monkeypatch.setitem(art._ART_BY_PRODUCT, "Ghost Card", "cards/card-does-not-exist.ans")
    art._exists.cache_clear()
    iface = _iface(product_name="Ghost Card")           # no chipset -> should fall to generic
    assert art._card_art(iface) is None
    assert art.art_path_for(iface) == art._GENERIC


# --- pool pick + label ------------------------------------------------------

def test_pick_primary_prefers_a_card_with_art():
    no_art = _iface(product_name="Nonesuch", chipset="NOPE")
    has_art = _iface(product_name=ALFA.AWUS036H)
    assert art.pick_primary([no_art, has_art]) is has_art
    assert art.pick_primary([no_art]) is no_art          # none have art -> first member
    assert art.pick_primary([]) is None


def test_pool_art_empty_is_generic():
    assert art.pool_art([]) == art._GENERIC
    assert art.pool_art([_iface(product_name=ALFA.AWUS036H)]) == "cards/card-awus036h.ans"


# --- BreathingArt.set_art (runtime swap) ------------------------------------

def test_breathing_art_set_art_swaps_and_noops():
    ba = art.BreathingArt("focus-card.ans")             # unmounted; drive by hand
    repaints = []
    ba._repaint = lambda: repaints.append(1)            # avoid update() on an unmounted widget

    ba.set_art("cards/card-pau06.ans")
    assert ba._name == "cards/card-pau06.ans"
    assert len(repaints) == 1

    ba.set_art("cards/card-pau06.ans")                  # same name -> no-op, no repaint
    assert len(repaints) == 1


# --- end-to-end: the screen's live sync on a mounted CardEndpoint ------------

async def test_sync_card_updates_mounted_endpoint():
    """_sync_card must repoint the art + relabel a *mounted* CardEndpoint (exercises the real
    BreathingArt.update() and Label.update(), i.e. the stale-label bug fix path)."""
    from textual.app import App

    from wifit3.ui.screens.focus_v2 import FocusViewV2
    from wifit3.ui.screens.focus_v2.art import BreathingArt
    from wifit3.ui.screens.focus_v2.card_endpoint import CardEndpoint

    member = SimpleNamespace(driver=SimpleNamespace(product_name="AWUS036H"),
                             product_name="AWUS036H", chipset="RTL8187L",
                             mac_address="00:11:22:33:44:55")

    class _Host(App):
        def on_mount(self) -> None:
            self.array = SimpleNamespace(members=[member])
            self.push_screen(FocusViewV2())

    app = _Host()
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause(0)
        scr = app.screen
        scr._sync_card()
        await pilot.pause(0)
        card = scr.query_one("#card", CardEndpoint)
        from wifit3.ui.screens.focus_v2.tx_picker import TxDevicePicker
        assert card.query_one(BreathingArt)._name == "cards/card-awus036h.ans"
        assert card.query_one(TxDevicePicker)._text == "AWUS036H"   # single card -> plain name
        assert card._last["#card-bssid"] == "00:11:22:33:44:55"   # single card -> its MAC shows


# --- every supported device resolves loadable art (no crashes) --------------

def test_every_supported_device_renders_art():
    """Every VID:PID in every chip's SUPPORTED_IDS must resolve to a real, loadable
    card art (its own or the generic fallback) without raising, so no supported
    device can crash the card endpoint. A DeviceID stands in for the interface:
    art selection only reads .driver / .product_name / .chipset via getattr."""
    from wifit3.device import manager

    manager.supported_ids.cache_clear()
    ids = manager.supported_ids()
    assert ids, "no supported devices were discovered"

    for (vid, pid), (entry, _key, _import) in sorted(ids.items()):
        who = f"{vid:#06x}:{pid:#06x} ({entry.product_name or entry.chipset})"
        path = art.art_path_for(entry)
        assert isinstance(path, str) and path, f"{who}: no art path"
        assert art._exists(path), f"{who}: art {path!r} is missing on disk"
        assert art.art_size(path)[0] > 0, f"{who}: art {path!r} rendered empty"
