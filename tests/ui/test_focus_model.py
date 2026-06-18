"""Pure-function tests for the shared Focus view-model (``ui.focus_model``).

These exercise the campaign-value derivations directly with light stubs — no
Textual, no interface — so the brains are pinned independent of either screen's
layout."""
import types

from wifit3.engine.attacks.wep.crack import CRACK_READY_THRESHOLD
from wifit3.ui import focus_model as fm


def _wep_ap(*, wep_key=None, persisted_wep=False, unique_ivs=0):
    persisted = []
    if persisted_wep:
        persisted = [types.SimpleNamespace(kind="WEP", value="6162636465", timestamp=0)]
    return types.SimpleNamespace(
        encryption="WEP", wep_key=wep_key, persisted=persisted,
        wep=types.SimpleNamespace(unique_ivs=unique_ivs),
        handshakes={}, wpa3=False, transition_mode=False,
    )


def _wep_camp(*, chop=False, cracker_samples=0, replay_state=None):
    return types.SimpleNamespace(
        chop_active=chop,
        cracker=types.SimpleNamespace(sample_count=cracker_samples),
        replay=types.SimpleNamespace(state=replay_state),
    )


def test_headline_persisted_wep_idle_shows_recovered():
    """An already-cracked AP, no campaign → the recovered-key banner."""
    h = fm.derive_headline(_wep_ap(persisted_wep=True), None, fm.Campaigns())
    assert "WEP key recovered" in h[0]


def test_headline_active_campaign_outranks_recovered_key():
    """Re-running Replay on an already-cracked AP must show LIVE progress (with
    the IV count), not the frozen 'recovered' banner — an active attack is the
    dominant activity."""
    ap = _wep_ap(persisted_wep=True, unique_ivs=1234)
    h = fm.derive_headline(ap, None, fm.Campaigns(wep=_wep_camp(replay_state="replaying")))
    joined = " ".join(h)
    assert "Replaying" in h[0]
    assert "recovered" not in joined.lower()
    assert "1,234" in joined


def test_headline_chop_and_crack_states():
    ap = _wep_ap(persisted_wep=True)
    chop = fm.derive_headline(ap, None, fm.Campaigns(wep=_wep_camp(chop=True)))
    assert "ChopChop" in chop[0]
    cracking = fm.derive_headline(
        ap, None, fm.Campaigns(wep=_wep_camp(cracker_samples=CRACK_READY_THRESHOLD)))
    assert "Cracking" in cracking[0]


def test_headline_cracking_names_the_concurrent_tx_action():
    """While cracking, the headline names BOTH the live TX action and the crack
    (replay/chop run concurrently and the action can change mid-crack)."""
    ap = _wep_ap(persisted_wep=True)
    crk = CRACK_READY_THRESHOLD
    replaying = fm.derive_headline(
        ap, None, fm.Campaigns(wep=_wep_camp(cracker_samples=crk, replay_state="replaying")))
    assert "Replaying ARP" in replaying[0] and "Cracking" in replaying[0]
    waiting = fm.derive_headline(
        ap, None, fm.Campaigns(wep=_wep_camp(cracker_samples=crk, replay_state="waiting-arp")))
    assert "Waiting for a packet" in waiting[0] and "Cracking" in waiting[0]
    chopping = fm.derive_headline(
        ap, None, fm.Campaigns(wep=_wep_camp(chop=True, cracker_samples=crk)))
    assert "Chopping a packet" in chopping[0] and "Cracking" in chopping[0]


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


def _wep_btn_ap():
    return types.SimpleNamespace(encryption="WEP", wps=None, wpa3=False,
                                 transition_mode=False, wps_locked=False)


def test_derive_buttons_wep_labels_and_variants():
    """Idle = ARP Replay (green) / ChopChop (blue, disabled until a campaign);
    running = Stop Replay (red) / Stop Chop (orange)."""
    idle = fm.derive_buttons(_wep_btn_ap(), fm.Campaigns())
    assert idle.gen_ivs.label == "ARP Replay" and idle.gen_ivs.variant == "success"
    assert idle.chop.label == "ChopChop" and idle.chop.disabled is True
    run = fm.derive_buttons(_wep_btn_ap(), fm.Campaigns(wep=_wep_camp(chop=True)))
    assert run.gen_ivs.label == "Stop Replay" and run.gen_ivs.variant == "error"
    assert run.chop.label == "Stop Chop" and run.chop.variant == "warning"
