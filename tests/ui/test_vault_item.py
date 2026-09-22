from datetime import datetime

from wifit3.ui.screens.vault_item import relative_time


def test_relative_time_singular_minute():
    now = int(datetime.now().timestamp())
    assert relative_time(now - 60) == "1 minute ago"


def test_relative_time_plural_minutes():
    now = int(datetime.now().timestamp())
    assert relative_time(now - 300) == "5 minutes ago"
