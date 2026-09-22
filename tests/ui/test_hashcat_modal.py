import wifit3.ui.vault.modals.hashcat as mod
from wifit3.ui.vault.modals.hashcat import _default_hashcat_path


def test_default_path_prefers_which(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/somewhere/hashcat")
    assert _default_hashcat_path() == "/somewhere/hashcat"


def test_default_path_falls_back_per_os(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert _default_hashcat_path() == "/usr/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    assert _default_hashcat_path() == "/opt/homebrew/bin/hashcat"
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert _default_hashcat_path() == r"C:\hashcat\hashcat.exe"
