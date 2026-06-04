"""DFS channel classification — the fact the scanner default-hop and the Channel Filter
[d]fs key both rely on."""
from wifit3.wlan.channels import is_dfs, scan_hop_order


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
