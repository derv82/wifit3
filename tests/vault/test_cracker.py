from wifit3.vault.tools.cracker import WordlistQueue


def test_queue_label_is_empty_without_wordlists():
    assert WordlistQueue().label == ""
