"""Unit tests for the pure + classification helpers in wifit3.setup.linux.

The live path (graphical pkexec → files written → kernel module unloaded → replug → cold card)
can't be exercised without a real Linux box + hardware, so it's left to the Kali smoke. The
deterministic logic — the rule/blacklist text, the privileged argv, and the install/remove
classification — is covered here on any OS by forcing the platform/euid via monkeypatch.

The live module-discovery tests are the exception: they stand up a *real* sysfs tree (colon-named
interface dirs like ``2-1:1.0`` + a ``driver`` symlink), which Windows' filesystem can't represent
(``:`` is illegal in a path component; symlinks need privilege). The production glob/readlink leans
on those exact Linux semantics, so faking them faithfully off-Linux isn't possible — ``_fake_sysfs``
skips on Windows, and these run on the Linux CI leg instead.
"""
import sys
from pathlib import Path

import pytest

import wifit3.setup.linux as lin
from wifit3.setup import SetupTarget
from wifit3.setup.linux import (
    LinuxSetupResult,
    _choose_escalation_method,
    blacklist_path,
    discover_kernel_modules,
    emit_blacklist_text,
    emit_udev_text,
    install_rule,
    kernel_driver_bound,
    remove_rule,
    rule_path,
    run_privileged,
)

AR9271 = (0x0cf3, 0x9271)


def _target(key="ar9271", ids=(AR9271,), hints=("ath9k_htc",)):
    return SetupTarget(key=key, description="Atheros AR9271 / ALFA AWUS036NHA",
                       ids=tuple(ids), module_hints=tuple(hints))


# --- fake sysfs for live module discovery ------------------------------------------------------

def _fake_sysfs(tmp_path, monkeypatch, *, sub="sys", vid=0x0cf3, pid=0x9271, bound="ath9k_htc",
                module=None,
                modalias="usb:v0CF3p9271d0108dc00dsc00dp00icFFiscFFipFFin00"):
    """Build a minimal /sys/bus/usb/devices tree for one card and point the module at it. ``sub``
    lets one test stand up several distinct trees under the same tmp_path."""
    if sys.platform == "win32":
        pytest.skip("fake sysfs needs colon-named interface dirs + a driver symlink — Linux-only "
                    "filesystem semantics (covered on the Linux CI leg)")
    base = tmp_path / sub
    dev = base / "2-1"
    dev.mkdir(parents=True)
    (dev / "idVendor").write_text(f"{vid:04x}\n")
    (dev / "idProduct").write_text(f"{pid:04x}\n")
    if modalias:
        (dev / "modalias").write_text(modalias + "\n")
    intf = dev / "2-1:1.0"
    intf.mkdir()
    if bound:
        drivers = base / "drivers" / bound
        drivers.mkdir(parents=True)
        (intf / "driver").symlink_to(drivers)
        if module:  # out-of-tree drivers expose driver/module -> /sys/module/<ko>
            moddir = base / "module" / module
            moddir.mkdir(parents=True)
            (drivers / "module").symlink_to(moddir)
    monkeypatch.setattr(lin, "SYSFS_USB", str(base))
    return base


def test_bound_modules_reads_the_interface_driver_symlink(tmp_path, monkeypatch):
    _fake_sysfs(tmp_path, monkeypatch, bound="ath9k_htc")
    assert lin._bound_modules([AR9271]) == {"ath9k_htc"}


def test_bound_modules_resolves_driver_to_module_name(tmp_path, monkeypatch):
    # Out-of-tree Realtek: the sysfs *driver* is ``rtl8814au`` but the *module* is ``8814au`` —
    # modprobe blacklists the module, so both names must surface (over-listing is harmless).
    _fake_sysfs(tmp_path, monkeypatch, bound="rtl8814au", module="8814au")
    assert lin._bound_modules([AR9271]) == {"rtl8814au", "8814au"}


def test_kernel_driver_bound_true_for_real_driver_false_for_usbfs(tmp_path, monkeypatch):
    _fake_sysfs(tmp_path, monkeypatch, sub="a", bound="ath9k_htc")
    assert kernel_driver_bound([AR9271]) is True
    # libusb-claimed (usbfs) or nothing bound is NOT a taint.
    _fake_sysfs(tmp_path, monkeypatch, sub="b", bound="usbfs")
    assert kernel_driver_bound([AR9271]) is False
    _fake_sysfs(tmp_path, monkeypatch, sub="c", bound=None)
    assert kernel_driver_bound([AR9271]) is False


def test_kernel_driver_bound_ignores_other_vidpid(tmp_path, monkeypatch):
    _fake_sysfs(tmp_path, monkeypatch, bound="ath9k_htc")
    assert kernel_driver_bound([(0x1234, 0x5678)]) is False


def test_resolve_via_modalias_runs_modprobe_R_on_the_cards_modalias(tmp_path, monkeypatch):
    _fake_sysfs(tmp_path, monkeypatch, bound=None,
                modalias="usb:v0CF3p9271d0108dc00")
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/sbin/modprobe")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        class R:  # noqa: D401
            stdout = "ath9k_htc\n"
        return R()

    monkeypatch.setattr(lin.subprocess, "run", fake_run)
    assert lin._resolve_via_modalias([AR9271]) == {"ath9k_htc"}
    assert seen["argv"][:2] == ["modprobe", "-R"]
    assert seen["argv"][2].startswith("usb:v0CF3p9271")


def test_discover_unions_bound_modalias_and_hint_and_drops_the_stack(tmp_path, monkeypatch):
    _fake_sysfs(tmp_path, monkeypatch, bound="rtw_8821au")
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/sbin/modprobe")
    monkeypatch.setattr(lin.subprocess, "run",
                        lambda argv, **kw: type("R", (), {"stdout": "88XXau\nmac80211\n"})())
    target = _target(key="rtl8821au", ids=(AR9271,), hints=("rtw_8821au",))
    mods = discover_kernel_modules(target)
    # bound ∪ modalias-resolved ∪ hint, with the shared stack (mac80211) removed.
    assert mods == ["88XXau", "rtw_8821au"]
    assert "mac80211" not in mods


def test_discover_falls_back_to_hint_when_card_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(lin, "SYSFS_USB", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(lin.shutil, "which", lambda n: None)  # no modprobe
    assert discover_kernel_modules(_target(hints=("ath9k_htc",))) == ["ath9k_htc"]


# --- file naming + text emitters ---------------------------------------------------------------

def test_paths_are_per_chipset_with_sort_safe_prefixes():
    assert rule_path("ar9271") == "/etc/udev/rules.d/60-wifit3-ar9271.rules"
    assert blacklist_path("ar9271") == "/etc/modprobe.d/wifit3-ar9271.conf"
    # key is sanitized before it lands in a privileged path.
    assert blacklist_path("../evil") == "/etc/modprobe.d/wifit3----evil.conf"


def test_emit_udev_text_is_per_chipset_grouped_and_inline_comment_free():
    text = emit_udev_text(_target(ids=(AR9271,)), "sudo")
    assert text.count('SUBSYSTEM=="usb"') == 1            # just this chipset, not the fleet
    assert 'ATTR{idVendor}=="0cf3"' in text and 'ATTR{idProduct}=="9271"' in text
    assert 'GROUP="sudo", MODE="0660"' in text
    for line in text.splitlines():
        if line.startswith("SUBSYSTEM"):
            assert "#" not in line                        # inline # makes udev drop the rule


def test_emit_blacklist_text_blacklists_and_neutralizes_each_module():
    text = emit_blacklist_text(_target(), ["ath9k_htc"])
    assert "blacklist ath9k_htc" in text
    assert "install ath9k_htc /bin/true" in text          # closes by-name / dependency loads


# --- privileged shell builders -----------------------------------------------------------------

def test_install_cmd_writes_both_files_unloads_module_and_grants_node():
    cmd = lin._install_cmd(tmp_rule="/t/r.rules", key="ar9271", tmp_blacklist="/t/b.conf",
                           modules=["ath9k_htc"], group="sudo", node="/dev/bus/usb/003/053")
    assert f"install -m 0644 /t/r.rules {rule_path('ar9271')}" in cmd
    assert f"install -m 0644 /t/b.conf {blacklist_path('ar9271')}" in cmd
    assert "udevadm control --reload-rules" in cmd
    assert "modprobe -r ath9k_htc" in cmd
    assert "(chgrp sudo /dev/bus/usb/003/053 || true) && (chmod 0660 /dev/bus/usb/003/053 || true)" in cmd


def test_install_cmd_root_skips_udev_rule_and_chgrp():
    # group=None (root): no access rule, no chgrp — only the blacklist matters to root.
    cmd = lin._install_cmd(tmp_rule=None, key="ar9271", tmp_blacklist="/t/b.conf",
                           modules=["ath9k_htc"], group=None, node="/dev/bus/usb/003/053")
    assert ".rules" not in cmd and "chgrp" not in cmd
    assert f"install -m 0644 /t/b.conf {blacklist_path('ar9271')}" in cmd


def test_remove_cmd_deletes_both_files_and_chowns_node_back():
    cmd = lin._remove_cmd("ar9271", "/dev/bus/usb/003/053")
    assert f"rm -f {rule_path('ar9271')} {blacklist_path('ar9271')}" in cmd
    assert "udevadm control --reload-rules" in cmd
    assert "chown root:root /dev/bus/usb/003/053" in cmd


# --- run_privileged / _choose_escalation_method ------------------------------------------------

def test_run_privileged_builds_pkexec_argv(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: f"/usr/bin/{n}")
    captured = {}
    monkeypatch.setattr(lin.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or lin.subprocess.CompletedProcess(argv, 0))
    assert run_privileged("echo hi", "pkexec") == 0
    assert captured["argv"] == ["/usr/bin/pkexec", "/usr/bin/sh", "-c", "echo hi"]


def test_run_privileged_missing_runner_returns_127(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: None)
    assert run_privileged("x", "pkexec") == 127


def test_choose_escalation_method_prefers_pkexec_then_sudo_then_none(monkeypatch):
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/x" if n in ("pkexec", "sudo") else None)
    assert _choose_escalation_method() == "pkexec"
    monkeypatch.setattr(lin.shutil, "which", lambda n: "/x" if n == "sudo" else None)
    assert _choose_escalation_method() == "sudo"
    monkeypatch.setattr(lin.shutil, "which", lambda n: None)
    assert _choose_escalation_method() is None


# --- install_rule classification ---------------------------------------------------------------

def _force_linux_nonroot(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(lin.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(lin, "_access_group", lambda: "sudo")  # deterministic; CI may lack sudo
    monkeypatch.setattr(lin, "discover_kernel_modules", lambda t: ["ath9k_htc"])


def test_install_rule_success_stages_pair_and_elevates(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_escalation_method", lambda: "pkexec")
    seen = {}
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: seen.update(cmd=cmd) or 0)
    r = install_rule(_target(), node="/dev/bus/usb/003/053")
    assert r.ok and not r.cancelled
    assert "replug" in r.message.lower()
    assert r.detail == blacklist_path("ar9271")
    rule = (tmp_path / "wifit3-ar9271.rules").read_text()
    conf = (tmp_path / "wifit3-ar9271.conf").read_text()
    assert rule.count('SUBSYSTEM=="usb"') == 1            # per-chipset, not the fleet
    assert "blacklist ath9k_htc" in conf
    assert "60-wifit3-ar9271.rules" in seen["cmd"] and "wifit3-ar9271.conf" in seen["cmd"]
    assert "chgrp sudo /dev/bus/usb/003/053" in seen["cmd"]


def test_install_rule_root_writes_blacklist_without_elevation(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(lin.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(lin, "discover_kernel_modules", lambda t: ["ath9k_htc"])
    seen = {}
    monkeypatch.setattr(lin, "_run_as_root", lambda cmd: seen.update(cmd=cmd) or 0)
    elevated = []
    monkeypatch.setattr(lin, "run_privileged", lambda c, m: elevated.append(c) or 0)
    r = install_rule(_target())
    assert r.ok and not elevated                          # root never elevates
    assert "wifit3-ar9271.conf" in seen["cmd"]            # blacklist still written as root
    assert ".rules" not in seen["cmd"]                    # but no access rule (root opens directly)
    assert not (tmp_path / "wifit3-ar9271.rules").exists()


def test_install_rule_fails_when_in_no_admin_group(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_access_group", lambda: None)
    called = []
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: called.append(cmd) or 0)
    r = install_rule(_target())
    assert not r.ok and not r.cancelled and not called
    assert "sudo or wheel" in r.message.lower()


def test_install_rule_declined_is_cancelled(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_escalation_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = install_rule(_target())
    assert not r.ok and r.cancelled


def test_install_rule_no_elevator_offers_manual_path(monkeypatch, tmp_path):
    _force_linux_nonroot(monkeypatch, tmp_path)
    monkeypatch.setattr(lin, "_choose_escalation_method", lambda: None)
    r = install_rule(_target())
    assert not r.ok and not r.cancelled
    assert "manually" in r.detail


def test_install_rule_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        install_rule(_target())


# --- remove_rule classification ----------------------------------------------------------------

def _point_paths_at_tmp(monkeypatch, tmp_path):
    rdir, bdir = tmp_path / "udev", tmp_path / "modprobe"
    rdir.mkdir()
    bdir.mkdir()
    monkeypatch.setattr(lin, "RULE_DIR", str(rdir))
    monkeypatch.setattr(lin, "BLACKLIST_DIR", str(bdir))
    return rdir, bdir


def test_remove_rule_absent_is_benign_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    _point_paths_at_tmp(monkeypatch, tmp_path)
    r = remove_rule(_target())
    assert r.ok and "nothing to remove" in r.message.lower()


def test_remove_rule_success_tells_user_to_replug(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    _point_paths_at_tmp(monkeypatch, tmp_path)
    Path(rule_path("ar9271")).write_text("x")             # one of the pair present is enough
    monkeypatch.setattr(lin, "_choose_escalation_method", lambda: "pkexec")
    seen = {}
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: seen.update(cmd=cmd) or 0)
    r = remove_rule(_target(), node="/dev/bus/usb/003/053")
    assert r.ok and "replug" in r.message.lower()
    assert "60-wifit3-ar9271.rules" in seen["cmd"] and "wifit3-ar9271.conf" in seen["cmd"]
    assert "chown root:root /dev/bus/usb/003/053" in seen["cmd"]


def test_remove_rule_declined_is_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(lin.sys, "platform", "linux")
    monkeypatch.setattr(lin.os, "geteuid", lambda: 1000, raising=False)
    _point_paths_at_tmp(monkeypatch, tmp_path)
    Path(blacklist_path("ar9271")).write_text("x")
    monkeypatch.setattr(lin, "_choose_escalation_method", lambda: "pkexec")
    monkeypatch.setattr(lin, "run_privileged", lambda cmd, method: 126)
    r = remove_rule(_target())
    assert not r.ok and r.cancelled


def test_remove_rule_non_linux_raises(monkeypatch):
    monkeypatch.setattr(lin.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        remove_rule(_target())


def test_linux_setup_result_defaults():
    r = LinuxSetupResult(ok=True, message="x")
    assert r.ok and not r.cancelled and r.detail is None
