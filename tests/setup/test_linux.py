"""Unit tests for the pure + classification helpers in wifit3.setup.linux.

The live path (graphical pkexec → udev rule written → node becomes writable → retry connect)
can't be exercised without a real Linux box + hardware, so it's left to the Kali smoke
(DEVICE-SETUP.md). Everything deterministic — rule text, the file-as-source-of-truth set math,
run_privileged argv, and grant/revoke result classification — is covered here and runs on any OS
by forcing the platform/euid via monkeypatch.
"""
import pytest

import wifit3.setup.linux as lin
from wifit3.setup.linux import (
    _DEFAULT_PERMS,
    _PERMS_CLAUSE,
    LinuxSetupResult,
    _choose_method,
    _rebuild_text,
    build_rule_text,
    emit_udev_text,
    granted_ids,
    grant_access,
    revoke_access,
    run_privileged,
    supported_count,
)


# --- rule text -------------------------------------------------------------------------

def test_build_rule_text_clause_hex_and_per_card_comment():
    text = build_rule_text([(0x0BDA, 0x8813, "RTL8814AU")], "all")
    assert 'SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="8813"' in text
    assert 'TAG+="uaccess", MODE="0660", GROUP="plugdev"' in text
    assert "# RTL8814AU" in text  # description on its own comment line


def test_build_rule_text_never_trails_a_comment_on_a_rule_line():
    # The bug that bit us: modern udev rejects the whole rule if a `#` trails it. A description
    # containing a `#` must still land on its own comment line, never inline.
    text = build_rule_text([(0x0BDA, 0x8813, "Card # with hash"), (0x148F, 0x5372, "Ralink")])
    for line in text.splitlines():
        if line.startswith("SUBSYSTEM"):
            assert "#" not in line


def test_build_rule_text_dedups_repeated_vidpid():
    text = build_rule_text([(0x0BDA, 0x8813, "a"), (0x0BDA, 0x8813, "b")])
    assert text.count('ATTR{idProduct}=="8813"') == 1


def test_perms_levers_ship_uaccess_plus_plugdev_not_loose():
    assert _DEFAULT_PERMS == "all"
    assert _PERMS_CLAUSE["all"] == 'TAG+="uaccess", MODE="0660", GROUP="plugdev"'
    assert _PERMS_CLAUSE["uaccess"] == 'TAG+="uaccess"'
    assert "loose" not in _PERMS_CLAUSE  # 0666 world-RW is probe-only, must not ship


def test_rule_path_is_shared_with_60_prefix():
    # One shared file (not per-VID:PID); 60- sorts before systemd's 70-uaccess.rules.
    assert lin.RULE_PATH == "/etc/udev/rules.d/60-wifit3.rules"


# --- file-as-source-of-truth: parse + rebuild round-trip -------------------------------

def test_granted_ids_parses_what_build_rule_text_wrote(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187"), (0x0CF3, 0x9271, "AR9271")]))
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    assert granted_ids() == {(0x0BDA, 0x8187), (0x0CF3, 0x9271)}


def test_granted_ids_absent_file_is_empty_set(monkeypatch, tmp_path):
    monkeypatch.setattr(lin, "RULE_PATH", str(tmp_path / "nope.rules"))
    assert granted_ids() == set()


def test_rebuild_text_blanket_header_only_when_full(monkeypatch):
    monkeypatch.setattr(lin, "_registry_ids",
                        lambda: [(0x1, 0x1, "a"), (0x2, 0x2, "b")])
    partial = _rebuild_text({(0x1, 0x1)})
    full = _rebuild_text({(0x1, 0x1), (0x2, 0x2)})
    assert "all supported cards" not in partial
    assert "all supported cards" in full


# --- run_privileged / _choose_method ---------------------------------------------------

def test_run_privileged_builds_pkexec_argv(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: f"/usr/bin/{n}")
    captured = {}

    def fake_call(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(lin.subprocess, "call", fake_call)
    assert run_privileged("echo hi", "pkexec") == 0
    assert captured["argv"] == ["/usr/bin/pkexec", "/usr/bin/sh", "-c", "echo hi"]


def test_run_privileged_missing_runner_returns_127(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: None)
    assert run_privileged("x", "pkexec") == 127


def test_choose_method_prefers_pkexec_then_sudo_then_none(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/x" if n in ("pkexec", "sudo") else None)
    assert _choose_method() == "pkexec"
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/x" if n == "sudo" else None)
    assert _choose_method() == "sudo"
    monkeypatch.setattr(lin.shutil, "which", lambda n: None)
    assert _choose_method() is None


# --- grant_access (one + all) ----------------------------------------------------------

def _force_linux_nonroot(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin.tempfile, "gettempdir", lambda: str(tmp_path))


def _capture_staged(monkeypatch):
    """Capture the rule text staged for the privileged install (the tmp file write)."""
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 0)


def test_grant_all_stages_the_whole_fleet(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    _capture_staged(monkeypatch)
    r = grant_access(None)
    assert r.ok and not r.cancelled
    assert r.detail == "/etc/udev/rules.d/60-wifit3.rules"
    staged = (tmp_path / "wifit3.rules").read_text()
    assert staged.count('SUBSYSTEM=="usb"') == supported_count() > 10
    assert "all supported cards" in staged           # blanket header
    assert str(supported_count()) in r.message


def test_grant_one_unions_into_existing(monkeypatch, tmp_path):
    # Card A already granted; granting card B must keep A.
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187")]))
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    _force_linux_nonroot(monkeypatch, tmp_path)
    _capture_staged(monkeypatch)
    monkeypatch.setattr(lin, "_registry_ids",
                        lambda: [(0x0BDA, 0x8187, "RTL8187"), (0x0CF3, 0x9271, "AR9271")])

    r = grant_access((0x0CF3, 0x9271, "AR9271"))
    assert r.ok and "AR9271" in r.message
    staged = (tmp_path / "wifit3.rules").read_text()
    assert 'ATTR{idProduct}=="8187"' in staged       # A preserved
    assert 'ATTR{idProduct}=="9271"' in staged       # B added


def test_grant_declined_is_cancelled(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = grant_access(None)
    assert not r.ok and r.cancelled


def test_grant_no_elevator_offers_manual_path(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: None)
    r = grant_access(None)
    assert not r.ok and not r.cancelled
    assert "sudo wifit3" in r.detail


def test_grant_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        grant_access(None)


# --- revoke_access (one + all) ---------------------------------------------------------

def test_revoke_all_absent_is_benign_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin, "RULE_PATH", str(tmp_path / "absent.rules"))
    r = revoke_access(None)
    assert r.ok and "nothing to remove" in r.message.lower()


def test_revoke_all_deletes_file_and_says_replug(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187")]))
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0, raising=False)  # root path: direct unlink
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin.subprocess, "call", lambda argv: 0)
    r = revoke_access(None)
    assert r.ok and "replug" in r.message.lower()
    assert not rule.exists()


def test_revoke_one_of_many_rewrites_without_it(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187"), (0x0CF3, 0x9271, "AR9271")]))
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin.subprocess, "call", lambda argv: 0)
    monkeypatch.setattr(lin, "_registry_ids",
                        lambda: [(0x0BDA, 0x8187, "RTL8187"), (0x0CF3, 0x9271, "AR9271")])

    r = revoke_access((0x0BDA, 0x8187, "RTL8187"))
    assert r.ok and "RTL8187" in r.message
    text = rule.read_text()
    assert 'ATTR{idProduct}=="8187"' not in text      # removed
    assert 'ATTR{idProduct}=="9271"' in text          # the other one stays


def test_revoke_last_one_deletes_the_file(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187")]))
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin.subprocess, "call", lambda argv: 0)
    r = revoke_access((0x0BDA, 0x8187, "RTL8187"))
    assert r.ok
    assert not rule.exists()                           # emptied → file dropped


def test_revoke_one_not_granted_is_benign(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187")]))
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    r = revoke_access((0x1234, 0x5678, "Never granted"))
    assert r.ok and "nothing to remove" in r.message.lower()
    assert rule.exists()                               # untouched


def test_revoke_declined_is_cancelled(monkeypatch, tmp_path):
    rule = tmp_path / "60-wifit3.rules"
    rule.write_text(build_rule_text([(0x0BDA, 0x8187, "RTL8187")]))
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = revoke_access(None)
    assert not r.ok and r.cancelled


def test_revoke_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        revoke_access(None)


# --- emit_udev_text (blanket, from the live registry) ----------------------------------

def test_emit_udev_text_is_blanket_and_registry_sourced():
    text = emit_udev_text()
    assert "all supported cards" in text
    assert 'ATTR{idVendor}=="0cf3"' in text           # AR9271 — a known supported card
    assert text.count('SUBSYSTEM=="usb"') == supported_count() > 10
    for line in text.splitlines():
        if line.startswith("SUBSYSTEM"):
            assert "#" not in line


def test_linux_setup_result_defaults():
    r = LinuxSetupResult(ok=True, message="x")
    assert r.ok and not r.cancelled and r.detail is None
