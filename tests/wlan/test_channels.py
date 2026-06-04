"""DFS channel classification — the fact the scanner default-hop and the Channel Filter
[d]fs key both rely on."""
from wifit3.wlan.channels import is_dfs


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
