"""Global test fixtures."""
import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.persist.config import Config


@pytest.fixture(autouse=True)
def _reset_campaign_active():
    Campaign.active = None   # before test
    yield
    Campaign.active = None   # after test


@pytest.fixture(autouse=True)
def _captures_to_tmp(tmp_path, monkeypatch):
    """Point Config.captures_dir at each test's tmp so nothing writes to ./captures."""
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
