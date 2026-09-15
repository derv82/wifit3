"""Tests for the shared Focus view-model (``ui.focus_model``).

Most exercise the campaign-value derivations directly with light stubs (no
Textual): the headline + status markup are pure functions that read the active
campaign off ``Campaign.active``. The button-matrix cases drive the real
``FocusViewV2.refresh_buttons`` against a headless screen, since button assembly
moved onto the screen (there is no ``derive_buttons`` any more)."""
import types

import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets import Button

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign
from wifit3.campaigns.pbc import WpsPbcCapture
from wifit3.campaigns.pin import WpsCampaign
from wifit3.campaigns.pmkid import PmkidHarvestAttack
from wifit3.campaigns.wep import WepCampaign
from wifit3.crack.wep import CRACK_READY_THRESHOLD
from wifit3.models import AccessPoint, Handshake, IdKey, IdSource
from wifit3.ui import focus_model as fm
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.persist.config import Config


@pytest.fixture(autouse=True)
def _reset_active():
    """campaign_blocked/refresh_buttons/derive_headline read Campaign.active: reset per test."""
    Campaign.active = None
    yield
    Campaign.active = None


def _running(key, **extra):
    """A stand-in for the active campaign (only .key + any extra attrs are read)."""
    return types.SimpleNamespace(key=key, **extra)


# ----- fake active campaigns: real subclasses so derive_headline / card_dynamic's
# isinstance() checks fire, but with only the attributes those readers touch. -----


class _FakeWep(WepCampaign):
    def __init__(self, *, chop=False, cracker_samples=0, replay_state=None, recovered_key=None):
        self.chop = types.SimpleNamespace(is_active=chop)   # chop_active is a property reading this
        self.cracker = types.SimpleNamespace(sample_count=cracker_samples)
        self.replay = types.SimpleNamespace(state=replay_state)
        self.recovered_key = recovered_key


class _FakeWps(WpsCampaign):
    def __init__(self, *, found_pin=None):
        self.state = types.SimpleNamespace(found_pin=found_pin)


class _FakeDeauth(DeauthCampaign):
    def __init__(self):
        pass


class _FakeEvilTwin(EvilTwinCampaign):
    def __init__(self, *, captured=False, twin_channel=1, fakeap=None):
        self.captured = captured
        self.twin_channel = twin_channel
        self.fakeap = fakeap


class _FakePmkid(PmkidHarvestAttack):
    def __init__(self):
        pass


class _FakePbc(WpsPbcCapture):
    def __init__(self):
        pass


def _wep_ap(*, wep_key=None, persisted_wep=False, unique_ivs=0):
    persisted = []
    if persisted_wep:
        persisted = [types.SimpleNamespace(type="WEP", value="6162636465", timestamp=0)]
    return types.SimpleNamespace(
        encryption="WEP", wep_key=wep_key, persisted=persisted,
        wep=types.SimpleNamespace(unique_ivs=unique_ivs),
        handshakes={}, wpa3=False, transition_mode=False, bssid="aa:bb:cc:dd:ee:ff",
    )


def _wpa_ap(*, known_psk=None):
    return types.SimpleNamespace(
        encryption="WPA2", wep_key=None, persisted=[], wep=None,
        handshakes={}, wpa3=False, transition_mode=False, known_psk=known_psk,
        bssid="aa:bb:cc:dd:ee:ff",
    )


def test_headline_persisted_wep_idle_shows_recovered():
    """An already-cracked AP, no campaign → the recovered-key banner."""
    h = fm.derive_headline(_wep_ap(persisted_wep=True), None)
    assert "WEP key recovered" in h[0]


def test_headline_active_campaign_outranks_recovered_key():
    """Re-running Replay on an already-cracked AP must show LIVE progress (with
    the IV count), not the frozen 'recovered' banner: an active attack is the
    dominant activity."""
    ap = _wep_ap(persisted_wep=True, unique_ivs=1234)
    Campaign.active = _FakeWep(replay_state="replaying")
    h = fm.derive_headline(ap, None)
    joined = " ".join(h)
    assert "Replaying" in h[0]
    assert "recovered" not in joined.lower()
    assert "1,234" in joined


def _pmkid_ap(pmkid_akm):
    hs = Handshake(bssid="aa:bb:cc:dd:ee:01", client_mac="11:22:33:44:55:66",
                   pmkid=bytes(16), pmkid_akm=pmkid_akm, beacon_frame=b"x")
    return types.SimpleNamespace(encryption="WPA2", wep_key=None, persisted=[], wep=None,
                                 known_psk=None, handshakes={"11:22:33:44:55:66": hs}, bssid="aa:bb:cc:dd:ee:ff")


def test_headline_sae_pmkid_is_not_a_captured_win():
    """A WPA3/SAE PMKID lands on the handshake but save_pmkid withholds it, so the
    headline must NOT claim 'Captured … saved' (the false-save bug)."""
    joined = " ".join(fm.derive_headline(_pmkid_ap(pmkid_akm=8), None))
    assert "Captured" not in joined and "PMKID ×" not in joined
    assert "saved to captures/" not in joined


def test_headline_psk_pmkid_is_a_captured_win():
    joined = " ".join(fm.derive_headline(_pmkid_ap(pmkid_akm=2), None))
    assert "Captured" in joined and "PMKID ×1" in joined


def test_headline_chop_and_crack_states():
    ap = _wep_ap(persisted_wep=True)
    Campaign.active = _FakeWep(chop=True)
    chop = fm.derive_headline(ap, None)
    assert "ChopChop" in chop[0]
    Campaign.active = _FakeWep(cracker_samples=CRACK_READY_THRESHOLD)
    cracking = fm.derive_headline(ap, None)
    assert "Cracking" in cracking[0]


def test_headline_cracking_names_the_concurrent_tx_action():
    """While cracking, the headline names BOTH the live TX action and the crack
    (replay/chop run concurrently and the action can change mid-crack)."""
    ap = _wep_ap(persisted_wep=True)
    crk = CRACK_READY_THRESHOLD
    Campaign.active = _FakeWep(cracker_samples=crk, replay_state="replaying")
    replaying = fm.derive_headline(ap, None)
    assert "Replaying ARP" in replaying[0] and "Cracking" in replaying[0]
    Campaign.active = _FakeWep(cracker_samples=crk, replay_state="waiting-arp")
    waiting = fm.derive_headline(ap, None)
    assert "Waiting for a packet" in waiting[0] and "Cracking" in waiting[0]
    Campaign.active = _FakeWep(chop=True, cracker_samples=crk)
    chopping = fm.derive_headline(ap, None)
    assert "Chopping a packet" in chopping[0] and "Cracking" in chopping[0]


def test_wps_status_shows_fail_reason():
    camp = types.SimpleNamespace(
        state=types.SimpleNamespace(found_pin=None, tested=0, phase="common", first_half=None),
        status="failed",
        fail_reason="WPS stayed locked for 5 cycles without PIN progress",
        eta_seconds=None,
    )

    line = fm.wps_status_markup(camp)

    assert "failed" in line
    assert "stayed locked" in line


def test_headline_recovered_wps_psk_shows_banner():
    """A recovered WPS PSK (PBC or PIN, after the campaign is torn down) shows a
    terminal banner instead of decaying back to 'Listening'."""
    h = fm.derive_headline(_wpa_ap(known_psk="hunter2"), None)
    assert "WPS PSK recovered" in h[0]


def test_headline_listening_when_no_psk():
    h = fm.derive_headline(_wpa_ap(known_psk=None), None)
    assert "Listening for handshake" in h[0]


def test_headline_live_pbc_outranks_listening():
    Campaign.active = _FakePbc()
    h = fm.derive_headline(_wpa_ap(known_psk=None), None)
    assert "PushButton" in h[0] and "capturing" in h[0].lower()


def test_headline_wps_pin_found_while_held_then_psk_after_teardown():
    # Campaign still held with a found PIN → cracked banner.
    Campaign.active = _FakeWps(found_pin="12345670")
    held = fm.derive_headline(_wpa_ap(known_psk=None), None)
    assert "WPS PIN cracked" in held[0]
    # After teardown the PSK lives on the AP → recovered banner (not Listening).
    Campaign.active = None
    after = fm.derive_headline(_wpa_ap(known_psk="hunter2"), None)
    assert "WPS PSK recovered" in after[0]


def _iface_with_usable(n):
    return types.SimpleNamespace(
        wep_store=types.SimpleNamespace(crack_sample_count=lambda bssid: n))


def test_wep_status_lines_idle_is_one_line_usable_only():
    """No campaign → a single usable-IVs line (red at 0), no fake-auth line."""
    ap = types.SimpleNamespace(bssid="aa:bb:cc:dd:ee:ff")
    lines = fm.wep_status_lines(ap, _iface_with_usable(0), None, 0)
    assert len(lines) == 1
    assert "Usable IVs:" in lines[0] and "[red]0[/red]" in lines[0]


def test_wep_status_lines_campaign_splits_fakeauth_and_ivs():
    """A running campaign → two separate lines (fake-auth, then usable IVs) so
    neither scrunches on a narrow terminal."""
    ap = types.SimpleNamespace(bssid="aa:bb:cc:dd:ee:ff")
    camp = types.SimpleNamespace(fake_auth=types.SimpleNamespace(
        state="associated", next_reauth_at=0, fail_reason=None))
    lines = fm.wep_status_lines(ap, _iface_with_usable(1234), camp, 0)
    assert len(lines) == 2
    assert "Fake-Auth:" in lines[0] and "Associated" in lines[0]
    assert "[cyan]1,234[/cyan]" in lines[1] and "Usable IVs:" in lines[1]


def test_wep_status_lines_drops_threshold_once_crossed():
    """/10k tags the goal while below it; once crossed the denominator is
    meaningless, so it's dropped and only the climbing count shows."""
    ap = types.SimpleNamespace(bssid="aa:bb:cc:dd:ee:ff")
    assert "/10k" in fm.wep_status_lines(ap, _iface_with_usable(9999), None, 0)[-1]
    crossed = fm.wep_status_lines(ap, _iface_with_usable(13982), None, 0)[-1]
    assert "/10k" not in crossed and "13,982" in crossed


# RSN AKM suite numbers (00-0F-AC:N), parallel to the human-readable `akms`.
_AKM_NUM = {"PSK": 2, "PSK-SHA256": 6, "SAE": 8, "802.1X": 1, "EAP": 1, "FT-PSK": 4}


def _rsn_ap(*, encryption="WPA2", akms=("PSK",), wpa3=False, transition_mode=False,
            pmf_required=False, pmf_capable=False, akm_suites=None, ssid="EvilNet",
            last_beacon_frame=b"\x80\x00beacon",
            wps=False, wps_locked=False, wps_version="1.0"):
    akms = list(akms)
    if akm_suites is None:
        akm_suites = [_AKM_NUM[a] for a in akms if a in _AKM_NUM]
    return types.SimpleNamespace(
        encryption=encryption, akms=akms, akm_suites=akm_suites, pairwise_cipher="CCMP",
        ssid=ssid, is_hidden=not (ssid and ssid != "<hidden>"), last_beacon_frame=last_beacon_frame,
        wpa3=wpa3, transition_mode=transition_mode, wep=None,
        pmf_required=pmf_required, pmf_capable=pmf_capable, bssid="aa:bb:cc:dd:ee:ff",
        wps=wps, wps_locked=wps_locked, wps_version=wps_version)


def test_pmf_status_markup_gradient():
    assert fm.pmf_status_markup(_rsn_ap(pmf_required=True, pmf_capable=True)) == "[red]Required[/red]"
    assert "dark_orange" in fm.pmf_status_markup(_rsn_ap(pmf_capable=True))
    assert fm.pmf_status_markup(_rsn_ap()) == "[dim]Disabled[/dim]"


def test_status_footer_wpa_shows_encryption_and_pmf():
    lines = fm.status_footer_lines(_rsn_ap(pmf_required=True, pmf_capable=True), None, None, 0)
    assert len(lines) == 2
    assert "Encryption:" in lines[0] and "WPA2" in lines[0]
    assert "PMF:" in lines[1] and "Required" in lines[1]
    assert "Protected Mgmt Frames" not in lines[1]      # abbreviated


def test_status_footer_combines_pmf_and_wps():
    """WPS rejoins the footer (it was dropped in v2) on the same row as PMF."""
    lines = fm.status_footer_lines(_rsn_ap(wps=True, wps_version="1.0"), None, None, 0)
    assert len(lines) == 2
    assert "PMF:" in lines[1] and "WPS:" in lines[1] and "1.0" in lines[1]


def test_router_identity_details_shows_source_provenance():
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "MikroTik")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "hAP ac²")
    details = fm.router_identity_details(ap)
    assert details is not None
    assert "[bold]MikroTik hAP ac²[/bold]" in details
    assert "[dim]Model:[/dim] hAP ac² [dim](WSC Beacon)[/dim]" in details
    assert "[dim]Manufacturer:[/dim] MikroTik [dim](WSC Beacon)[/dim]" in details


def test_router_identity_details_can_show_m1_and_oui_separately():
    ap = AccessPoint(bssid="00:03:93:11:22:33")
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Cisco")
    ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "AP-500")

    details = fm.router_identity_details(ap)
    assert details is not None
    assert "[bold]Cisco AP-500[/bold]" in details
    assert "[dim]Model:[/dim] AP-500 [dim](WSC M1)[/dim]" in details
    assert "[dim]Manufacturer:[/dim] Cisco [dim](WSC M1)[/dim]" in details
    assert "[dim]IEEE OUI:[/dim] Apple" in details


def test_router_identity_details_asus_wsc_beacon():
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    ap.identity.set(IdSource.OUI, IdKey.MANUFACTURER, "ASUSTek COMPUTER")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "ASUSTeK Computer Inc.")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Wi-Fi Protected Setup Router")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, "RT-AC66U")

    assert fm.router_identity_details(ap) == "\n".join([
        "[bold]ASUS RT-AC66U[/bold]",
        "[dim]Model:[/dim] RT-AC66U [dim](WSC Beacon)[/dim]",
        "[dim]Manufacturer:[/dim] ASUS [dim](WSC Beacon)[/dim]",
        "[dim]IEEE OUI:[/dim] ASUS",
    ])


def test_router_identity_details_netgear_wsc_m1():
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    ap.identity.set(IdSource.OUI, IdKey.MANUFACTURER, "Netgear")
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Netgear")
    ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "Netgear")
    ap.identity.set(IdSource.WSC_M1, IdKey.DEVICE_NAME, "C3700-100NAS")

    assert fm.router_identity_details(ap) == "\n".join([
        "[bold]Netgear C3700-100NAS[/bold]",
        "[dim]Model:[/dim] C3700-100NAS [dim](WSC M1)[/dim]",
        "[dim]Manufacturer:[/dim] Netgear [dim](WSC M1)[/dim]",
        "[dim]IEEE OUI:[/dim] Netgear",
    ])


def test_router_identity_details_odm_wsc_beacon_with_branded_oui():
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    ap.identity.set(IdSource.OUI, IdKey.MANUFACTURER, "Jensen Scandinavia AS")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Ralink Technology, Corp.")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Ralink Wireless Access Point")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, "Jensen of Scandinavia Air:Link 5000AC")

    assert fm.router_identity_details(ap) == "\n".join([
        "[bold]Jensen of Scandinavia Air:Link 5000AC[/bold]",
        "[dim]Model:[/dim] Jensen of Scandinavia Air:Link 5000AC [dim](WSC Beacon)[/dim]",
        "[dim]Manufacturer:[/dim] Jensen [dim](IEEE OUI)[/dim]",
        "[dim]Chipset:[/dim] Ralink [dim](WSC)[/dim]",
    ])


def test_router_identity_details_includes_device_type():
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "HP")
    ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "OfficeJet Pro")
    ap.identity.set(IdSource.WSC_M1, IdKey.DEVICE_TYPE, "printer")

    details = fm.router_identity_details(ap)
    assert details == "\n".join([
        "[bold]HP OfficeJet Pro (printer)[/bold]",
        "[dim]Model:[/dim] OfficeJet Pro [dim](WSC M1)[/dim]",
        "[dim]Manufacturer:[/dim] HP [dim](WSC M1)[/dim]",
        "[dim]Device Type:[/dim] printer [dim](WSC M1)[/dim]",
    ])


def test_router_identity_details_is_blank_without_evidence():
    assert fm.router_identity_details(AccessPoint(bssid="02:00:00:00:00:01")) is None


def test_status_footer_open_is_encryption_only():
    ap = types.SimpleNamespace(
        encryption="OPEN", akms=[], pairwise_cipher=None, wpa3=False,
        transition_mode=False, wep=None, pmf_required=False, pmf_capable=False, bssid="x")
    lines = fm.status_footer_lines(ap, None, None, 0)
    assert len(lines) == 1 and "Encryption:" in lines[0]


def test_status_footer_wep_is_fakeauth_and_usable_ivs():
    ap = types.SimpleNamespace(
        encryption="WEP", akms=[], pairwise_cipher=None, wpa3=False,
        transition_mode=False, wep=types.SimpleNamespace(unique_ivs=0), bssid="x")
    lines = fm.status_footer_lines(ap, _iface_with_usable(5), None, 0)
    assert any("Usable IVs" in ln for ln in lines)
    assert not any("Encryption:" in ln for ln in lines)


def _wep_btn_ap():
    return types.SimpleNamespace(encryption="WEP", wps=None, wpa3=False,
                                 transition_mode=False, wps_locked=False, is_hidden=False,
                                 ssid="WepNet", akm_suites=[], bssid="aa:bb:cc:dd:ee:ff", last_beacon_frame=b"\x80\x00beacon")


def _wep_hidden_ap():
    ap = _wep_btn_ap()
    ap.ssid, ap.is_hidden = None, True
    return ap


# ----- button matrix: driven through the real FocusViewV2.refresh_buttons -----


class _ButtonHost(App):
    """Headless host that mounts FocusViewV2 with no target, so the button tests can
    point it at a stub AP and call refresh_buttons directly."""
    def __init__(self):
        super().__init__()
        self.array = None
        self.target_ap = None
        self.pbc_enabled = True

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def buttons_screen():
    app = _ButtonHost()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.screen._tick_timer.stop()
        yield app.screen


_BTN_IDS = ("btn-gen-ivs", "btn-chop", "btn-deauth", "btn-pmkid", "btn-wps-pin", "btn-eviltwin")


def _buttons(screen, ap):
    """Drive the real refresh_buttons for ``ap``; return ``{button_id: Button}``."""
    screen.app.target_ap = ap
    screen.refresh_buttons()
    return {bid: screen.query_one(f"#{bid}", Button) for bid in _BTN_IDS}


def _bs(b):
    """(visible, disabled, label, variant): compact button-state tuple."""
    return (b.display, b.disabled, str(b.label), b.variant)


@pytest.mark.asyncio(loop_scope="module")
async def test_derive_buttons_wep_labels_and_variants(buttons_screen):
    """Idle = ARP Replay (green) / ChopChop (blue, disabled until a campaign);
    running = Stop Replay (red) / Stop Chop (orange)."""
    idle = _buttons(buttons_screen, _wep_btn_ap())
    assert str(idle["btn-gen-ivs"].label) == "ARP Replay" and idle["btn-gen-ivs"].variant == "success"
    assert str(idle["btn-chop"].label) == "ChopChop" and idle["btn-chop"].disabled is True
    Campaign.active = _FakeWep(chop=True)
    run = _buttons(buttons_screen, _wep_btn_ap())
    assert str(run["btn-gen-ivs"].label) == "Stop Replay" and run["btn-gen-ivs"].variant == "error"
    assert str(run["btn-chop"].label) == "Stop Chop" and run["btn-chop"].variant == "warning"


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_wpa2_psk_no_wps_pmkid_and_deauth_visible(buttons_screen):
    b = _buttons(buttons_screen, _rsn_ap(akms=("PSK",), wps=False))
    assert _bs(b["btn-pmkid"]) == (True, False, "PMKID", "primary")
    assert _bs(b["btn-deauth"]) == (True, False, "AutoDeauth", "primary")
    assert b["btn-wps-pin"].display is False
    assert b["btn-gen-ivs"].display is False and b["btn-chop"].display is False


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_deauth_hidden_for_sae_open_wep_and_pmf(buttons_screen):
    """Deauth shows only for a confirmed PSK-family AKM with PMF off: SAE-only,
    open, WEP and PMF-Required all hide it (deauth can't provoke a crackable PSK
    handshake, or PMF protects the frame)."""
    assert _buttons(buttons_screen, _rsn_ap(akms=("PSK",)))["btn-deauth"].display is True
    assert _buttons(buttons_screen, _rsn_ap(akms=("SAE",)))["btn-deauth"].display is False
    assert _buttons(buttons_screen, _rsn_ap(encryption="OPEN", akms=()))["btn-deauth"].display is False
    assert _buttons(buttons_screen, _wep_btn_ap())["btn-deauth"].display is False
    assert _buttons(buttons_screen, _rsn_ap(akms=("PSK",), pmf_required=True))["btn-deauth"].display is False


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_wpa2_wps_unlocked_pin_enabled(buttons_screen):
    b = _buttons(buttons_screen, _rsn_ap(wps=True, wps_locked=False))
    assert _bs(b["btn-wps-pin"]) == (True, False, "WPS PIN", "primary")
    assert b["btn-pmkid"].display is True and b["btn-pmkid"].disabled is False


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_wpa2_wps_locked_pin_visible_but_disabled(buttons_screen):
    b = _buttons(buttons_screen, _rsn_ap(wps=True, wps_locked=True))
    assert _bs(b["btn-wps-pin"]) == (True, True, "WPS PIN", "primary")


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_hidden_ssid_disables_assoc_attacks(buttons_screen):
    """A hidden AP (no known SSID) can't be associated, so every auth/assoc button is
    visible-but-disabled with a hidden-SSID reason: PMKID, WPS PIN, WEP fake-auth."""
    ap = _rsn_ap(akms=("PSK",), wps=True, ssid=None)
    b = _buttons(buttons_screen, ap)
    assert b["btn-pmkid"].disabled is True and "hidden" in fm.campaign_blocked(PmkidHarvestAttack, ap)
    assert b["btn-wps-pin"].disabled is True and "hidden" in fm.campaign_blocked(WpsCampaign, ap)
    wap = _wep_hidden_ap()
    wb = _buttons(buttons_screen, wap)
    assert wb["btn-gen-ivs"].disabled is True and "hidden" in fm.campaign_blocked(WepCampaign, wap)


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_hidden_leaves_deauth_enabled(buttons_screen):
    """Deauth spoofs addresses (no association), so a hidden SSID does not disable it."""
    b = _buttons(buttons_screen, _rsn_ap(akms=("PSK",), ssid=None))
    assert b["btn-deauth"].display is True and b["btn-deauth"].disabled is False


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_wpa3_transition_shows_pmkid_and_eviltwin(buttons_screen):
    b = _buttons(buttons_screen, _rsn_ap(wpa3=True, transition_mode=True))
    assert b["btn-pmkid"].display is True and b["btn-pmkid"].disabled is False
    assert b["btn-eviltwin"].display is True


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_wpa3_only_sae_shows_eviltwin(buttons_screen):
    """SAE-only: PMKID isn't crackable, no transition/WPS → those hide. EvilTwin still shows:
    it applies to any RSN incl. pure WPA3 (it herds SAE clients to a PSK twin)."""
    b = _buttons(buttons_screen,
                 _rsn_ap(encryption="WPA3", wpa3=True, transition_mode=False, akms=("SAE",)))
    assert all(not b[bid].display for bid in
               ("btn-gen-ivs", "btn-chop", "btn-pmkid", "btn-deauth", "btn-wps-pin"))
    assert b["btn-eviltwin"].display is True


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_mutex_running_wps_disables_siblings(buttons_screen):
    ap = _rsn_ap(wpa3=True, transition_mode=True, wps=True)
    Campaign.active = _FakeWps()
    b = _buttons(buttons_screen, ap)
    assert _bs(b["btn-wps-pin"]) == (True, False, "Stop PIN", "error")
    assert b["btn-pmkid"].disabled is True               # radio owned by WPS
    assert _bs(b["btn-eviltwin"]) == (True, True, "EvilTwin", "primary")


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_running_eviltwin_toggles_and_blocks_pmkid(buttons_screen):
    ap = _rsn_ap(wpa3=True, transition_mode=True)
    Campaign.active = _FakeEvilTwin()
    b = _buttons(buttons_screen, ap)
    assert _bs(b["btn-eviltwin"]) == (True, False, "Stop EvilTwin", "error")
    assert b["btn-pmkid"].disabled is True


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_running_pmkid_shows_stop_and_blocks_others(buttons_screen):
    """PMKID is now a stoppable, radio-owning campaign: while it runs it shows a
    Stop button AND (the flip) blocks the sibling attacks."""
    ap = _rsn_ap(wpa3=True, transition_mode=True, wps=True)
    Campaign.active = _FakePmkid()
    b = _buttons(buttons_screen, ap)
    assert _bs(b["btn-pmkid"]) == (True, False, "Stop PMKID", "error")
    assert b["btn-wps-pin"].disabled is True
    assert b["btn-eviltwin"].disabled is True


def test_other_long_running_tx_mutex_and_excludes():
    assert fm.other_long_running_tx() is False
    Campaign.active = _running("wep")
    assert fm.other_long_running_tx() is True
    assert fm.other_long_running_tx(exclude="wep") is False
    Campaign.active = _running("pbc")
    assert fm.other_long_running_tx() is True
    assert fm.other_long_running_tx(exclude="pbc") is False
    Campaign.active = _running("wps")
    assert fm.other_long_running_tx(exclude="wpa3down") is True


def test_deauth_blocked_by_mutex_or_pmf():
    assert fm.deauth_blocked(_rsn_ap()) is False
    assert fm.deauth_blocked(_rsn_ap(pmf_required=True)) is True
    Campaign.active = _running("wep")
    assert fm.deauth_blocked(_rsn_ap()) is True


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_open_hides_pmkid(buttons_screen):
    """THE FIX: an open network has no PSK AKM → no PMKID button (was shown)."""
    b = _buttons(buttons_screen, _rsn_ap(encryption="OPEN", akms=()))
    assert b["btn-pmkid"].display is False
    assert all(not b[bid].display for bid in
               ("btn-gen-ivs", "btn-chop", "btn-deauth", "btn-wps-pin", "btn-eviltwin"))


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_unconfirmed_encryption_shows_pmkid_disabled_with_reason(buttons_screen):
    """A hidden AP heard without a beacon RSN (encryption 'Unknown', no AKM) shows
    PMKID *disabled with a reason* instead of a silently-missing button, so the user
    knows WHY. A confirmed-open AP still hides it (test_buttons_open_hides_pmkid)."""
    ap = _rsn_ap(encryption="Unknown", akms=())
    st = _buttons(buttons_screen, ap)["btn-pmkid"]
    assert st.display is True and st.disabled is True
    reason = fm.campaign_blocked(PmkidHarvestAttack, ap)
    assert reason and "confirm" in reason.lower()
    # A confirmed-PSK AP is enabled with no reason.
    ok_ap = _rsn_ap(akms=("PSK",))
    ok = _buttons(buttons_screen, ok_ap)["btn-pmkid"]
    assert ok.disabled is False and fm.campaign_blocked(PmkidHarvestAttack, ok_ap) is None


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_enterprise_hides_pmkid(buttons_screen):
    """802.1X (enterprise) PMK isn't dictionary-crackable → no PMKID button."""
    b = _buttons(buttons_screen, _rsn_ap(akms=("802.1X",)))
    assert b["btn-pmkid"].display is False


def test_card_dynamic_each_state():
    assert fm.card_dynamic() == ""
    Campaign.active = _FakeWep()
    assert fm.card_dynamic() == "● replaying"
    Campaign.active = _FakeWep(chop=True)
    assert fm.card_dynamic() == "● chopping"
    Campaign.active = _FakeWps()
    assert fm.card_dynamic() == "● WPS PIN"
    Campaign.active = _FakeDeauth()
    assert fm.card_dynamic() == "● Deauth"
    Campaign.active = _FakeEvilTwin()
    assert fm.card_dynamic() == "● EvilTwin"
    Campaign.active = _FakePbc()
    assert fm.card_dynamic() == "● WPS PBC"


@pytest.mark.asyncio(loop_scope="module")
async def test_buttons_eviltwin_enabled_single_card(buttons_screen):
    ap = _rsn_ap(akms=("PSK",))
    st = _buttons(buttons_screen, ap)["btn-eviltwin"]
    assert st.display is True and st.disabled is False and fm.campaign_blocked(EvilTwinCampaign, ap) is None


def test_headline_eviltwin_active_and_captured():
    stats = types.SimpleNamespace(auth=2, assoc=1, m2=0, probes_direct=3, probes_wildcard=5)
    camp = _FakeEvilTwin(captured=False, twin_channel=1,
                         fakeap=types.SimpleNamespace(stats=stats))
    Campaign.active = camp
    active = fm.derive_headline(_rsn_ap(), None)
    assert "EvilTwin active" in active[0] and "CH 1" in active[0]
    assert "auth:2" in active[1] and "assoc:1" in active[1]
    assert "3 direct" in active[2] and "5 wildcard" in active[2]
    camp.captured = True
    assert "Captured" in fm.derive_headline(_rsn_ap(), None)[0]


@pytest.mark.asyncio(loop_scope="module")
async def test_derive_buttons_all_disabled_when_silenced(buttons_screen, monkeypatch):
    """Silencing an AP disables every campaign button (deauth included)."""
    ap = _rsn_ap(akms=("PSK",))
    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid])
    btns = _buttons(buttons_screen, ap)
    for bid in ("btn-gen-ivs", "btn-pmkid", "btn-deauth", "btn-wps-pin",
                "btn-eviltwin", "btn-chop"):
        assert btns[bid].disabled is True
    assert fm.campaign_blocked(DeauthCampaign, ap) == "AP silenced"


def test_headline_silenced_outranks_listening(monkeypatch):
    ap = _wpa_ap(known_psk=None)
    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid])
    assert "Silenced" in fm.derive_headline(ap, None)[0]
