"""The Vault filter match: case, width and accent folding."""
from wifit3.ui.screens.vault_table import matches_filter


def test_matches_filter_folds_case_width_and_accents():
    assert matches_filter("cafe", ["Café Net"])
    assert matches_filter("CAFÉ", ["cafe"])
    assert matches_filter("ｈｏｍｅ", ["HomeNet"])
    assert matches_filter("ee:ff", ["Net", "aa:bb:cc:dd:ee:ff"])
    assert matches_filter("wep_key", ["Net", "Net_aa-bb-cc-dd-ee-ff_1000_wep_key.txt"])
    assert not matches_filter("office", ["HomeNet", "aa:bb:cc:dd:ee:ff"])
