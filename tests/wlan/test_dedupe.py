"""Tests for the multicard RX deduplicator (wlan/dedupe.py).

Drives StreamMerger with explicit timestamps (no clock, no hardware): novel vs cross-card copy
inside/outside the window, the FC+addr+seq key (retry bit and seq step must NOT merge), the
coverage tallies (both / first / only), and dynamic add/remove of sources for hotplug."""

from wifit3.wlan.dedupe import StreamMerger


def _frame(*, fc=b"\x80\x00", dur=b"\x00\x00", a1=b"\x11" * 6, a2=b"\x22" * 6,
           a3=b"\x33" * 6, seq=b"\x00\x00", tail=b""):
    """A 24+ byte MPDU. key() reads fc(2) + a1/a2/a3(18) + seq(2); dur is deliberately excluded."""
    return fc + dur + a1 + a2 + a3 + seq + tail


A, B, C = "wlan0", "wlan1", "wlan2"


def test_cross_card_copy_is_suppressed_within_window():
    m = StreamMerger([A, B], window=0.3)
    assert m.submit(A, _frame(), 0.0) is True     # novel
    assert m.submit(B, _frame(), 0.1) is False    # B's copy of the same air
    assert m.novel == 1 and m.dup == 1 and m.both == 1
    assert m.first == {A: 1, B: 0}
    assert m.rx == {A: 1, B: 1}


def test_same_key_after_window_is_novel_again():
    m = StreamMerger([A, B], window=0.3)
    assert m.submit(A, _frame(), 0.0) is True
    assert m.submit(B, _frame(), 0.5) is True      # outside window: sequence reuse is not a copy
    assert m.novel == 2 and m.dup == 0 and m.both == 0


def test_same_source_repeat_counts_as_dup_but_not_both():
    m = StreamMerger([A], window=0.3)
    m.submit(A, _frame(), 0.0)
    assert m.submit(A, _frame(), 0.05) is False    # same card twice = still a dup
    assert m.dup == 1 and m.both == 0              # 'both' needs two DISTINCT sources


def test_duration_field_is_ignored_by_key():
    m = StreamMerger([A, B], window=0.3)
    m.submit(A, _frame(dur=b"\x00\x00"), 0.0)
    # Same transmission, per-receiver Duration/ID differs: must still merge.
    assert m.submit(B, _frame(dur=b"\xff\xff"), 0.1) is False


def test_retry_bit_is_a_distinct_frame():
    m = StreamMerger([A], window=0.3)
    m.submit(A, _frame(fc=b"\x80\x00"), 0.0)
    # Retry bit (0x08 in FC byte1) flips the key: a retransmission is its own frame.
    assert m.submit(A, _frame(fc=b"\x80\x08"), 0.05) is True
    assert m.novel == 2


def test_seq_step_is_a_distinct_frame():
    m = StreamMerger([A], window=0.3)
    m.submit(A, _frame(seq=b"\x00\x10"), 0.0)
    assert m.submit(A, _frame(seq=b"\x00\x20"), 0.05) is True
    assert m.novel == 2


def test_short_frame_falls_back_to_whole_buffer():
    m = StreamMerger([A], window=0.3)
    assert StreamMerger.key(b"\x01\x02\x03") == b"\x01\x02\x03"
    m.submit(A, b"\x01\x02\x03", 0.0)
    assert m.submit(A, b"\x01\x02\x03", 0.05) is False   # identical short buffer dedups


def test_evict_tallies_single_source_only():
    m = StreamMerger([A, B], window=0.3)
    m.submit(A, _frame(a2=b"\xaa" * 6), 0.0)          # only A hears this one
    m.submit(A, _frame(a2=b"\xbb" * 6), 0.0)          # both hear this one
    m.submit(B, _frame(a2=b"\xbb" * 6), 0.05)
    m.evict(1.0)                                       # both keys now older than the window
    assert m.only == {A: 1, B: 0}                      # the A-only frame counts; the shared one does not


def test_flush_finalizes_only_tally():
    m = StreamMerger([A, B], window=0.3)
    m.submit(A, _frame(a2=b"\xaa" * 6), 0.0)
    m.submit(B, _frame(a2=b"\xbb" * 6), 0.0)
    m.flush()
    assert m.only == {A: 1, B: 1}
    assert m._seen == {}


def test_three_sources_both_bumps_once_on_second_distinct():
    m = StreamMerger([A, B, C], window=0.3)
    assert m.submit(A, _frame(), 0.00) is True
    assert m.submit(B, _frame(), 0.05) is False       # A,B -> both += 1
    assert m.submit(C, _frame(), 0.10) is False       # A,B,C -> heard by >2, both stays 1
    assert m.both == 1 and m.dup == 2 and m.novel == 1


def test_add_and_remove_source():
    m = StreamMerger([A], window=0.3)
    m.add_source(B)
    assert m.rx == {A: 0, B: 0} and m.first == {A: 0, B: 0} and m.only == {A: 0, B: 0}
    m.submit(A, _frame(), 0.0)
    m.submit(B, _frame(), 0.1)                          # B copies A -> {A,B} in the key
    m.remove_source(B)
    assert B not in m.rx and B not in m.first and B not in m.only
    # The departed card is gone from the in-window key, so eviction sees a single-source frame.
    m.evict(1.0)
    assert m.only == {A: 1}


def test_submit_auto_registers_unknown_source():
    """The array passes a card id (iface.name) that may not have been pre-registered."""
    m = StreamMerger(window=0.3)
    assert m.submit("wlanX", _frame(), 0.0) is True
    assert m.rx["wlanX"] == 1 and m.first["wlanX"] == 1
