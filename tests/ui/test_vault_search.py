"""The Vault search match: case, width and accent folding."""
from wifit3.ui.screens.vault_table import matches_search


def test_matches_search_folds_case_width_and_accents():
    assert matches_search("cafe", ["Café Net"])
    assert matches_search("CAFÉ", ["cafe"])
    assert matches_search("ｈｏｍｅ", ["HomeNet"])
    assert matches_search("ee:ff", ["Net", "aa:bb:cc:dd:ee:ff"])
    assert matches_search("wep_key", ["Net", "Net_aa-bb-cc-dd-ee-ff_1000_wep_key.txt"])
    assert not matches_search("office", ["HomeNet", "aa:bb:cc:dd:ee:ff"])
