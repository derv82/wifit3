"""Termux detection, used to pick the USB backend and skip desktop setup."""
import sys

from wifit3.host import is_termux


def test_termux_version_env(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    assert is_termux()


def test_termux_prefix(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert is_termux()


def test_android_platform(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(sys, "platform", "android")
    assert is_termux()


def test_desktop_is_not_termux(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.setenv("PREFIX", "/usr")
    monkeypatch.setattr(sys, "platform", "linux")
    assert not is_termux()
