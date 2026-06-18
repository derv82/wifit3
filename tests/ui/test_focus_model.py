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


def _wep_camp(*, chop=False, cracker_samples=0):
    return types.SimpleNamespace(
        chop_active=chop,
        cracker=types.SimpleNamespace(sample_count=cracker_samples),
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
    h = fm.derive_headline(ap, None, fm.Campaigns(wep=_wep_camp()))
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
