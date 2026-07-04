"""Unit tests for the deferred EAPOL → handshake-tree aggregator.

Pure + clock-injected, so no UI / no hardware: feed CaptureEvents + a fake
``now`` and assert on the rendered markup lines.
"""
from wifit3.ui.capture_events import CaptureEvent, CaptureKind
from wifit3.ui.eapol_aggregate import EapolAggregator

_BSSID = "11:22:33:44:55:66"
_CLIENT = "aa:bb:cc:dd:ee:ff"


def _eapol(msg, *, client=_CLIENT, nonce=True, mic=True, eapol=True, pmkid=None):
    return CaptureEvent(
        kind=CaptureKind.EAPOL, bssid=_BSSID, client_mac=client, msg_num=msg,
        has_nonce=nonce, has_mic=mic, eapol_complete=eapol, has_pmkid=pmkid,
    )


def _hs(pair="M1+M2", client=_CLIENT):
    return CaptureEvent(kind=CaptureKind.HANDSHAKE, bssid=_BSSID,
                        client_mac=client, pair_label=pair)


def test_complete_handshake_flushes_one_tree():
    agg = EapolAggregator()
    agg.on_eapol(_eapol(1, pmkid=True), now=0.0)
    agg.on_eapol(_eapol(2), now=0.1)
    lines = agg.on_handshake(_hs("M1+M2"), now=0.1)
    text = "\n".join(lines)
    assert _CLIENT in text and "4-Way Handshake" in text       # header names the client
    assert "M1" in text and "AP→Client" in text                # M1 direction
    assert "M2" in text and "AP←Client" in text                # M2 direction
    assert "PMKID" in text                                      # M1's PMKID tick
    assert "Valid 4-Way Handshake (M1+M2)" in text
    assert len(lines) == 4                                      # header + M1 + M2 + verdict


def test_save_hint_folds_in_as_the_closing_leaf():
    agg = EapolAggregator()
    agg.on_eapol(_eapol(1, pmkid=True), now=0.0)
    agg.on_eapol(_eapol(2), now=0.1)
    hint = "[dim]saved: captures/NETGEAR2G_…_handshake.hc22000[/dim]"
    lines = agg.on_handshake(_hs("M1+M2"), now=0.1, save_hint=hint)
    # header + M1 + M2 + verdict(branch) + save(leaf) → the verdict is no longer
    # the terminal node; the save note closes the tree.
    assert len(lines) == 5
    assert "saved: captures/" in lines[-1]
    assert "Valid 4-Way Handshake" in "\n".join(lines)


def test_aggregates_repeated_messages_with_count():
    agg = EapolAggregator()
    for i in range(20):
        agg.on_eapol(_eapol(1), now=i * 0.01)
    flushed = agg.tick(now=10.0)                                # quiet > settle
    assert len(flushed) == 1
    text = "\n".join(flushed[0])
    assert "M1 ×20" in text
    assert "Valid 4-Way Handshake" not in text                 # partial → no verdict


def test_partial_waits_for_settle_then_flushes_once():
    agg = EapolAggregator(settle_s=3.0)
    agg.on_eapol(_eapol(1), now=0.0)
    assert agg.tick(now=2.9) == []                             # not settled yet
    flushed = agg.tick(now=3.0)
    assert len(flushed) == 1 and "M1" in "\n".join(flushed[0])
    assert agg.tick(now=10.0) == []                            # burst consumed


def test_suppress_until_new_instance():
    agg = EapolAggregator()
    agg.on_eapol(_eapol(1), now=0.0)
    agg.on_eapol(_eapol(2), now=0.1)
    first = agg.on_handshake(_hs("M1+M2"), now=0.1)
    assert "capture ×" not in "\n".join(first)                 # first capture, no ×N

    # Retransmitted M3/M4 of the captured exchange are swallowed (no new burst).
    agg.on_eapol(_eapol(3), now=0.2)
    agg.on_eapol(_eapol(4), now=0.3)
    assert agg.tick(now=100.0) == []

    # A genuinely new instance completing re-announces compactly as ×2.
    second = agg.on_handshake(_hs("M2+M3"), now=5.0)
    text = "\n".join(second)
    assert "Valid 4-Way Handshake (M2+M3)" in text and "capture ×2" in text
    assert len(second) == 1                                    # one-liner, no tree


def test_reset_makes_a_client_brand_new_again():
    agg = EapolAggregator()
    agg.on_eapol(_eapol(1), now=0.0)
    agg.on_handshake(_hs(), now=0.0)
    agg.reset()
    agg.on_eapol(_eapol(1), now=1.0)
    lines = agg.on_handshake(_hs("M1+M2"), now=1.0)
    assert "capture ×" not in "\n".join(lines)                 # not a re-announce
    assert len(lines) >= 2                                     # a fresh tree


def test_representative_prefers_the_most_complete_frame():
    agg = EapolAggregator()
    agg.on_eapol(_eapol(1, pmkid=False), now=0.0)             # M1 without PMKID …
    agg.on_eapol(_eapol(1, pmkid=True), now=0.1)             # … then one WITH it
    text = "\n".join(agg.tick(now=10.0)[0])
    assert "M1 ×2" in text and "PMKID[green]✓" in text        # rep = the richer M1
