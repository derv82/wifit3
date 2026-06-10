"""DFS channel classification — the fact the scanner default-hop and the Channel Filter
[d]fs key both rely on."""
from wifit3.wlan.channels import (
    _compress_runs,
    band_label,
    band_ranges,
    is_dfs,
    scan_hop_order,
)


def test_dfs_covers_unii2_and_unii2e():
    # UNII-2 (52-64) + UNII-2e (100-144) are DFS (radar-shared).
    for ch in (52, 56, 60, 64, 100, 116, 132, 140, 144):
        assert is_dfs(ch), ch


def test_non_dfs_5ghz():
    # UNII-1 (36-48) and UNII-3 (149-165) are NOT DFS.
    for ch in (36, 40, 44, 48, 149, 153, 157, 161, 165):
        assert not is_dfs(ch), ch


def test_24ghz_is_not_dfs():
    for ch in range(1, 15):
        assert not is_dfs(ch), ch


def test_default_hop_split_matches_mainline_non_dfs():
    # The DKMS driver's 25-channel 5 GHz set splits into the 9 non-DFS channels (the hop
    # default + exactly mainline's list) and 16 DFS channels (opt-in).
    dkms_5g = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
               128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
    non_dfs = [c for c in dkms_5g if not is_dfs(c)]
    assert non_dfs == [36, 40, 44, 48, 149, 153, 157, 161, 165]
    assert sum(1 for c in dkms_5g if is_dfs(c)) == 16


def test_scan_hop_order_front_loads_busy_24ghz():
    # Sequential SUPPORTED_CHANNELS → 1/6/11 first, then rest of 2.4, then 5 GHz.
    got = scan_hop_order(list(range(1, 14)) + [36, 40, 44, 48, 149])
    assert got == [1, 6, 11, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13, 36, 40, 44, 48, 149]


def test_scan_hop_order_is_a_permutation():
    # Pure reorder: same channels in, same channels out (no adds/drops).
    src = list(range(1, 14)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]
    assert sorted(scan_hop_order(src)) == sorted(src)


def test_scan_hop_order_dedupes_keeping_first():
    assert scan_hop_order([6, 6, 1, 1, 11]) == [1, 6, 11]


def test_scan_hop_order_partial_priority_set():
    # Only the priority channels actually present are front-loaded.
    assert scan_hop_order([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]
    assert scan_hop_order([2, 3, 6, 4]) == [6, 2, 3, 4]


# --- band label / range compression (scanner init line + Channel Filter log) --------

def test_band_label():
    assert band_label(list(range(1, 14))) == "2.4 GHz"
    assert band_label([36, 40, 44, 48, 149]) == "5 GHz"
    assert band_label([1, 6, 11, 36, 149]) == "2.4 GHz + 5 GHz"
    assert band_label([]) == ""


def test_compress_runs_24ghz_step1():
    assert _compress_runs(list(range(1, 14)), 1) == "1-13"
    assert _compress_runs([1, 2, 3, 4, 5, 6, 11], 1) == "1-6, 11"
    assert _compress_runs([1], 1) == "1"
    assert _compress_runs([], 1) == ""


def test_compress_runs_5ghz_step4():
    # 5 GHz channels are spaced by 4, so 36,40,44,48 collapse to one run; the big
    # gap to UNII-3 (and any excluded DFS slot) breaks it.
    assert _compress_runs([36, 40, 44, 48, 149, 153, 157, 161, 165], 4) == "36-48, 149-165"
    assert _compress_runs([44], 4) == "44"
    # DFS included: UNII-1+2 are contiguous (36-64), UNII-2e separate (100-144).
    full = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
            128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
    assert _compress_runs(full, 4) == "36-64, 100-144, 149-165"


def test_band_ranges_always_per_band():
    # The chosen rule: every case breaks out per band, each with its own ranges.
    assert band_ranges(list(range(1, 14))) == [("2.4 GHz", "1-13")]
    assert band_ranges([36, 40, 44, 48, 149, 153, 157, 161, 165]) == [
        ("5 GHz", "36-48, 149-165")
    ]
    assert band_ranges(list(range(1, 14)) + [36, 40, 44, 48, 149, 153, 157, 161, 165]) == [
        ("2.4 GHz", "1-13"),
        ("5 GHz", "36-48, 149-165"),
    ]
    # A custom cross-band filter stays per-band (not a merged range run).
    assert band_ranges([1, 2, 3, 4, 5, 6, 11, 44]) == [
        ("2.4 GHz", "1-6, 11"),
        ("5 GHz", "44"),
    ]
