"""DFS is opt-in: WIFIT3_DFS=1 enables the full 5 GHz table; the default stays non-DFS."""
import wifit3.chips.rtl8821au_dkms.driver as driver
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver


def test_class_channels_follow_the_toggle_at_import():
    assert Rtl8821auDkmsDriver.SUPPORTED_CHANNELS == driver._channels()


def test_dfs_toggle(monkeypatch):
    monkeypatch.delenv("WIFIT3_DFS", raising=False)
    assert 52 not in driver._channels()
    assert 116 not in driver._channels()
    assert 36 in driver._channels()

    monkeypatch.setenv("WIFIT3_DFS", "1")
    assert 52 in driver._channels()
    assert 116 in driver._channels()
    assert 36 in driver._channels()
