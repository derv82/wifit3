"""Unit tests for the pure + classification helpers in wifit3.setup.linux.

The live path (graphical pkexec → udev rule written → node becomes writable → retry connect)
can't be exercised without a real Linux box + hardware, so it's left to the Kali smoke
(DEVICE-SETUP.md). Everything deterministic — rule text, run_privileged argv, and the
install/remove result classification — is covered here and runs on any OS by forcing the
platform/euid via monkeypatch.
"""
import pytest

import wifit3.setup.linux as lin
from wifit3.setup.linux import (
    LinuxSetupResult,
    _choose_method,
    build_rule_text,
    emit_udev_text,
    install_rule,
    remove_rule,
    run_privileged,
)


# --- rule text -------------------------------------------------------------------------

def test_build_rule_text_clause_hex_and_per_card_comment():
    text = build_rule_text([(0x0BDA, 0x8813, "RTL8814AU")], "sudo")
    assert 'SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="8813"' in text
    assert 'GROUP="sudo", MODE="0660"' in text
    assert "# RTL8814AU" in text  # description on its own comment line


def test_build_rule_text_never_trails_a_comment_on_a_rule_line():
    # The bug that bit us: modern udev rejects the whole rule if a `#` trails it. A description
    # containing a `#` must still land on its own comment line, never inline.
    text = build_rule_text([(0x0BDA, 0x8813, "Card # with hash"), (0x148F, 0x5372, "Ralink")], "sudo")
    for line in text.splitlines():
        if line.startswith("SUBSYSTEM"):
            assert "#" not in line


def test_build_rule_text_dedups_repeated_vidpid():
    text = build_rule_text([(0x0BDA, 0x8813, "a"), (0x0BDA, 0x8813, "b")], "sudo")
    assert text.count('ATTR{idProduct}=="8813"') == 1


def test_rule_clause_is_group_scoped_no_uaccess_or_world_rw():
    # An admin group owns the node (pure udevd chgrp — applies/reverts under --settle, no logind
    # ACL to lag or linger). No uaccess, no OWNER (udev silently drops a uid-1000 owner), no 0666.
    text = build_rule_text([(0x0BDA, 0x8813, "x")], "sudo")
    assert 'GROUP="sudo", MODE="0660"' in text
    assert "uaccess" not in text
    assert "OWNER" not in text
    assert "0666" not in text


def test_install_rule_loud_fails_when_in_no_admin_group(monkeypatch, tmp_path):
    # No sudo/wheel membership → refuse up front rather than write a rule udev silently drops
    # (the OWNER="uid-1000" trap) and time out later.
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_access_group", lambda: None)
    called = []
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: called.append(cmd) or 0)
    r = install_rule()
    assert not r.ok and not r.cancelled and not called  # never elevated
    assert "sudo or wheel" in r.message.lower()


def test_install_rule_as_root_writes_no_rule(monkeypatch):
    # Zero-install path: root opens + detaches the node directly, so no rule is written.
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0, raising=False)
    called = []
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: called.append(cmd) or 0)
    r = install_rule()
    assert r.ok and not called  # never elevated, never wrote a file
    assert "root" in r.message.lower()


def test_rule_path_is_shared_blanket_with_60_prefix():
    # One shared file (not per-VID:PID); 60- sorts after 50-udev-default so our OWNER wins.
    assert lin.RULE_PATH == "/etc/udev/rules.d/60-wifit3.rules"


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


# --- install_rule classification (blanket) ---------------------------------------------

def _force_linux_nonroot(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(lin, "_access_group", lambda: "sudo")  # deterministic, CI may lack sudo


def test_install_rule_success_stages_blanket_and_elevates(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    seen = {}

    def fake_priv(cmd, method):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(lin, "run_privileged", fake_priv)
    r = install_rule()
    assert r.ok and not r.cancelled
    assert r.detail == "/etc/udev/rules.d/60-wifit3.rules"
    staged = (tmp_path / "wifit3.rules").read_text()
    assert staged.count('SUBSYSTEM=="usb"') > 10        # blanket: the whole fleet
    assert "60-wifit3.rules" in seen["cmd"]             # install shell targets the shared file


def test_install_rule_declined_is_cancelled(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = install_rule()
    assert not r.ok and r.cancelled


def test_install_rule_no_elevator_offers_manual_path(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: None)
    r = install_rule()
    assert not r.ok and not r.cancelled
    assert "sudo wifit3" in r.detail


def test_install_rule_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        install_rule()


# --- remove_rule classification (blanket) ----------------------------------------------

def test_remove_rule_absent_is_benign_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin, "RULE_PATH", str(tmp_path / "absent.rules"))
    r = remove_rule()
    assert r.ok and "nothing to remove" in r.message.lower()


def test_remove_rule_success_tells_user_to_replug(monkeypatch, tmp_path):
    rule = tmp_path / "present.rules"
    rule.write_text("x")
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 0)
    r = remove_rule()
    assert r.ok and "replug" in r.message.lower()


def test_remove_rule_declined_is_cancelled(monkeypatch, tmp_path):
    rule = tmp_path / "present.rules"
    rule.write_text("x")
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = remove_rule()
    assert not r.ok and r.cancelled


def test_remove_rule_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        remove_rule()


# --- live node chgrp/chown (the no-replug propagation) ---------------------------------

def test_install_shell_chgrps_the_live_node():
    cmd = lin._install_shell("/tmp/wifit3.rules", "sudo", "/dev/bus/usb/003/053")
    assert f"install -m 0644 /tmp/wifit3.rules {lin.RULE_PATH}" in cmd
    assert "udevadm control --reload-rules" in cmd
    assert "chgrp sudo /dev/bus/usb/003/053 && chmod 0660 /dev/bus/usb/003/053" in cmd
    # no udevadm trigger: it re-applies a rule's MODE but not its GROUP, so it can't grant the
    # live node — the chgrp is what does.
    assert "trigger" not in cmd


def test_install_shell_without_node_only_writes_the_rule():
    cmd = lin._install_shell("/tmp/wifit3.rules", "sudo", None)
    assert "chgrp" not in cmd and "chmod" not in cmd       # no live node to grant (CLI)


def test_remove_shell_chowns_node_back_to_root():
    # The bug this guards: removal used to leave the granted group on the live node until a
    # physical replug. udev applies a rule but never un-applies it, so revoke must chown by hand.
    cmd = lin._remove_shell("/dev/bus/usb/003/053")
    assert f"rm -f {lin.RULE_PATH}" in cmd
    assert "chown root:root /dev/bus/usb/003/053" in cmd
    assert "trigger" not in cmd


def test_install_rule_chgrps_the_card_node(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    seen = {}

    def fake_priv(cmd, method):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(lin, "run_privileged", fake_priv)
    install_rule(node="/dev/bus/usb/003/053")
    assert "chgrp sudo /dev/bus/usb/003/053 && chmod 0660 /dev/bus/usb/003/053" in seen["cmd"]


def test_remove_rule_chowns_live_node_to_root(monkeypatch, tmp_path):
    rule = tmp_path / "present.rules"
    rule.write_text("x")
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin, "RULE_PATH", str(rule))
    monkeypatch.setattr(lin, "_choose_method", lambda: "pkexec")
    seen = {}

    def fake_priv(cmd, method):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(lin, "run_privileged", fake_priv)
    remove_rule(node="/dev/bus/usb/003/053")
    assert "chown root:root /dev/bus/usb/003/053" in seen["cmd"]


# --- emit_udev_text (blanket, from the live registry) ----------------------------------

def test_emit_udev_text_is_blanket_and_registry_sourced():
    text = emit_udev_text()
    assert "all supported cards" in text
    assert 'ATTR{idVendor}=="0cf3"' in text          # AR9271 — a known supported card
    assert text.count('SUBSYSTEM=="usb"') > 10        # the whole fleet, not one card
    for line in text.splitlines():
        if line.startswith("SUBSYSTEM"):
            assert "#" not in line


def test_linux_setup_result_defaults():
    r = LinuxSetupResult(ok=True, message="x")
    assert r.ok and not r.cancelled and r.detail is None
