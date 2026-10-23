"""Global test fixtures."""
import itertools
from pathlib import Path

import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.persist.config import Config, CpuCrackSettings, HashcatSettings, HashtopolisSettings

_test_dir_numbers = itertools.count()


@pytest.fixture(autouse=True)
def _reset_campaign_active():
    Campaign.active = None   # before test
    yield
    Campaign.active = None   # after test


@pytest.fixture(scope="session")
def _test_dirs_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("test_dirs")


@pytest.fixture
def _test_dir(request: pytest.FixtureRequest, _test_dirs_root: Path) -> Path:
    """The test's tmp_path if it uses one, else a fresh empty dir: tmp_path lists every earlier
    test's dir before making its own, which costs seconds across the suite."""
    if "tmp_path" in request.fixturenames:
        return request.getfixturevalue("tmp_path")
    path = _test_dirs_root / str(next(_test_dir_numbers))
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _captures_to_tmp(_test_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Config.captures_dir at each test's own dir so nothing writes to ./captures."""
    monkeypatch.setattr(Config, "captures_dir", str(_test_dir))


@pytest.fixture(autouse=True)
def _fresh_tool_settings(monkeypatch):
    """Each test starts from the default cracking-tool tables, so one test's edits can't leak."""
    monkeypatch.setattr(Config, "cpu_crack", CpuCrackSettings())
    monkeypatch.setattr(Config, "hashcat", HashcatSettings())
    monkeypatch.setattr(Config, "hashtopolis", HashtopolisSettings())
