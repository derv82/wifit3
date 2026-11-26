import pytest

import wifit3.ui.vault.modals.hashcat as mod
from wifit3.persist.config import Config, ConfigError, HashcatSettings
from wifit3.ui.vault.modals.hashcat import HashcatConfigModal, _default_hashcat_path


@pytest.fixture(autouse=True)
def _unset_saved_paths(monkeypatch):
    """These live on Config as process-wide state; pin them so test order can't matter."""
    monkeypatch.setattr(Config.hashcat, "path", None)
    monkeypatch.setattr(Config.hashcat, "wordlists", [])
    monkeypatch.setattr(Config.hashcat, "args", None)


def test_default_path_falls_back_per_os(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert _default_hashcat_path() == "/usr/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert _default_hashcat_path() == "/opt/homebrew/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert _default_hashcat_path() == r"C:\hashcat\hashcat.exe"


def test_saved_path_outranks_a_hashcat_found_on_path(monkeypatch):
    """Someone who points wifit3 at a specific build must not be silently overridden by
    whatever happens to be on $PATH."""
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    monkeypatch.setattr(Config.hashcat, "path", r"D:\tools\hashcat\hashcat.exe")
    assert _default_hashcat_path() == r"D:\tools\hashcat\hashcat.exe"


def test_launching_remembers_the_launch_settings():
    """Straight through the real Config.save(): tests/ui/conftest.py points it at a tmp file."""
    modal = HashcatConfigModal(probed_ssids=["CafeGuest"])
    modal._wordlists = [r"D:\rockyou.txt", r"D:\top29m.txt"]
    modal._remember({"hashcat_exe": r"D:\hashcat.exe",
                     "wordlists": [r"D:\rockyou.txt", r"D:\top29m.txt"],
                     "hashcat_args": "-w 3", "default_passwords": False,
                     "ap_ssid_passwords": True, "probed_ssid_passwords": False})
    Config.hashcat = HashcatSettings()
    Config.load()

    assert Config.hashcat == HashcatSettings(
        path=r"D:\hashcat.exe", args="-w 3", wordlists=[r"D:\rockyou.txt", r"D:\top29m.txt"],
        default_passwords=False, ap_ssid_passwords=True, probed_ssid_passwords=False)


def test_an_unwritable_config_warns_but_keeps_the_paths(monkeypatch):
    """A failed save must not cost the user their launch."""
    def _boom(cls):
        raise ConfigError("disk full")
    monkeypatch.setattr(Config, "save", classmethod(_boom))

    modal = HashcatConfigModal()
    warnings = []
    monkeypatch.setattr(modal, "notify", lambda *a, **k: warnings.append((a, k)))
    modal._remember({"hashcat_exe": r"D:\hashcat.exe", "wordlists": [r"D:\rockyou.txt"],
                     "hashcat_args": "", "default_passwords": True, "ap_ssid_passwords": True,
                     "probed_ssid_passwords": True})

    assert Config.hashcat.path == r"D:\hashcat.exe"
    assert warnings and warnings[0][1].get("title") == "Config Error"


