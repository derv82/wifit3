"""Channel-filter modal labels."""
from wifit3.ui.screens.channel_filter import ChannelFilterDialog


def test_channel_labels_name_the_band_or_dfs():
    assert ChannelFilterDialog._format_label(6) == "CH   6   (2.4 GHz)"
    assert ChannelFilterDialog._format_label(36) == "CH  36   (5 GHz)"
    assert ChannelFilterDialog._format_label(56) == "CH  56   (DFS)"
