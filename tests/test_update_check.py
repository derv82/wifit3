import json

import pytest

import wifit3.updates as updates
from wifit3.updates import ReleaseAsset, ReleaseInfo, UpdateCheckError, UpdateInfo, check_for_updates


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_version_key_accepts_tags_and_suffixes():
    assert updates._version_key("v1.2.3") == (1, 2, 3)
    assert updates._version_key("1.2.3") == (1, 2, 3)
    assert updates._version_key("1.2.3-rc1") == (1, 2, 3)


def test_version_key_rejects_unusable_versions():
    assert updates._version_key("distributing") == (0, 0, 0)
    assert updates._version_key("") == (0, 0, 0)


def _release(version, prerelease=False, assets=()):
    return ReleaseInfo(version, f"https://example/{version}", prerelease, tuple(assets))


def test_check_for_updates_reports_newer_release(monkeypatch):
    monkeypatch.setattr(updates, "_fetch_releases", lambda _timeout: (
        _release("0.1.3"), _release("0.1.4")))

    info = check_for_updates("0.1.3")

    assert info == UpdateInfo("0.1.3", "0.1.4", True, "https://example/0.1.4", True, True)


def test_check_for_updates_reports_current_release(monkeypatch):
    monkeypatch.setattr(updates, "_fetch_releases", lambda _timeout: (_release("0.1.3"),))

    info = check_for_updates("0.1.3")

    assert info.update_available is False
    assert info.current_version_known is True
    assert info.ran_from_source is True


def test_check_for_updates_flags_nonstandard_current_version(monkeypatch):
    monkeypatch.setattr(updates, "_fetch_releases", lambda _timeout: (_release("0.1.3"),))

    info = check_for_updates("0.1.3-dev")

    assert info.update_available is False
    assert info.current_version_known is False
    assert info.ran_from_source is True


def test_fetch_latest_release_parses_github_response(monkeypatch):
    monkeypatch.setattr(updates, "urlopen", lambda _req, timeout: _Response([{
        "tag_name": "v0.1.4", "html_url": "https://github.com/derv82/wifit3/releases/tag/v0.1.4",
        "prerelease": False, "assets": []}]))

    assert updates._fetch_latest_release(1.0) == (
        "0.1.4", "https://github.com/derv82/wifit3/releases/tag/v0.1.4")


def test_fetch_latest_release_rejects_missing_version(monkeypatch):
    monkeypatch.setattr(updates, "urlopen", lambda _req, timeout: _Response([{"html_url": "https://example"}]))

    with pytest.raises(UpdateCheckError):
        updates._fetch_latest_release(1.0)


def test_plan_update_selects_matching_linux_asset(monkeypatch):
    asset = ReleaseAsset("wifit3-linux-x64", "https://example/download")
    monkeypatch.setattr(updates, "_fetch_releases", lambda _timeout: (
        _release("0.1.3"), _release("0.1.4", assets=(asset,))))

    plan = updates.plan_update("0.1.3", system="Linux", machine="x86_64")

    assert plan.update.update_available is True
    assert plan.asset_name == "wifit3-linux-x64"
    assert plan.asset_url == "https://example/download"
    assert plan.auto_update_enabled is False


def test_plan_update_force_selects_asset_for_dev_version(monkeypatch):
    asset = ReleaseAsset("wifit3-linux-x64", "https://example/download")
    monkeypatch.setattr(updates, "_fetch_releases", lambda _timeout: (_release("0.1.4", assets=(asset,)),))

    plan = updates.plan_update("0.1.4-dev", force=True, system="Linux", machine="x86_64")

    assert plan.update.update_available is False
    assert plan.update.current_version_known is False
    assert plan.asset_name == "wifit3-linux-x64"


def test_update_current_binary_noops_outside_bundled_binary():
    result = updates.update_current_binary("0.1.3")

    assert result.updated is False
    assert "not running from a bundled binary" in result.message


def test_update_current_binary_refuses_dev_version_without_force(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.4-dev", "0.1.4", False, "https://example/release", False),
        "wifit3-linux-x64", "https://example/download"))

    result = updates.update_current_binary("0.1.4-dev", executable_path=exe)

    assert result.updated is False
    assert exe.read_bytes() == b"old"
    assert "--force" in result.message


def test_update_current_binary_replaces_executable(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-linux-x64", "https://example/download"))
    monkeypatch.setattr(updates, "_download_file", lambda _url, path, _timeout: path.write_bytes(b"new"))

    result = updates.update_current_binary("0.1.3", executable_path=exe)

    assert result.updated is True
    assert exe.read_bytes() == b"new"


def test_update_current_binary_stages_windows_helper(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3.exe"
    exe.write_bytes(b"old")
    launched = []
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-windows-x64.exe", "https://example/download"))
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.sys, "argv", [str(exe), "--update"])
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(updates.os, "access", lambda *_args: True)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **_kwargs: launched.append(args))
    monkeypatch.setattr(updates, "_download_file", lambda _url, path, _timeout: path.write_bytes(b"new"))

    result = updates.update_current_binary("0.1.3", executable_path=exe)

    assert result.updated is True
    assert result.restart_handled is True
    assert exe.read_bytes() == b"old"
    assert (tmp_path / "wifit3-updates" / "wifit3.exe.download").read_bytes() == b"new"
    assert launched == [["cmd.exe", "/c", str(tmp_path / "wifit3-updates" / f"wifit3-update-{updates.os.getpid()}.cmd")]]


def test_update_current_binary_uses_uac_for_protected_windows_path(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3.exe"
    exe.write_bytes(b"old")
    shell_calls = []
    shell32 = type("Shell32", (), {"ShellExecuteW": lambda self, *args: shell_calls.append(args) or 33})()
    windll = type("Windll", (), {"shell32": shell32})()
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-windows-x64.exe", "https://example/download"))
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(updates.os, "access", lambda *_args: False)
    monkeypatch.setattr(updates.ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(updates, "_download_file", lambda _url, path, _timeout: path.write_bytes(b"new"))

    result = updates.update_current_binary("0.1.3", executable_path=exe)

    assert result.updated is True
    assert result.restart_handled is True
    assert shell_calls
    assert shell_calls[0][1:3] == ("runas", "cmd.exe")


def test_update_current_binary_reports_permission_denied_when_uac_is_disabled(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-windows-x64.exe", "https://example/download"))
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(updates.os, "access", lambda *_args: False)
    monkeypatch.setattr(updates, "_download_file", lambda _url, path, _timeout: path.write_bytes(b"new"))

    result = updates.update_current_binary("0.1.3", executable_path=exe, allow_elevation=False)

    assert result.updated is False
    assert result.message == "permission denied replacing binary"


    exe = tmp_path / "wifit3"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-linux-x64", "https://example/download"))
    monkeypatch.setattr(updates, "_download_file", lambda _url, _path, _timeout: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setattr(updates.shutil, "which", lambda _cmd: None)

    result = updates.update_current_binary("0.1.3", force=True, executable_path=exe)

    assert result.updated is False
    assert result.message == "permission denied replacing binary"


def test_update_current_binary_uses_polkit_on_permission_denied(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3"
    exe.write_bytes(b"old")
    calls = []
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-linux-x64", "https://example/download"))
    monkeypatch.setattr(updates, "_download_file", lambda _url, _path, _timeout: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(updates.shutil, "which", lambda _cmd: "/usr/bin/pkexec")
    monkeypatch.setattr(updates.subprocess, "run", lambda args, **_kwargs: calls.append(args) or type(
        "Result", (), {"returncode": 0})())

    result = updates.update_current_binary("0.1.3", force=True, executable_path=exe)

    assert result.updated is True
    assert "elevated privileges" in result.message
    assert calls == [["/usr/bin/pkexec", str(exe), "--update", "--no-polkit", "--force"]]


def test_update_current_binary_reports_permission_denied_when_polkit_fails(tmp_path, monkeypatch):
    exe = tmp_path / "wifit3"
    exe.write_bytes(b"old")
    monkeypatch.setattr(updates, "plan_update", lambda *_args, **_kwargs: updates.UpdatePlan(
        UpdateInfo("0.1.3", "0.1.4", True, "https://example/release", True),
        "wifit3-linux-x64", "https://example/download"))
    monkeypatch.setattr(updates, "_download_file", lambda _url, _path, _timeout: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(updates.shutil, "which", lambda _cmd: "/usr/bin/pkexec")
    monkeypatch.setattr(updates.subprocess, "run", lambda *_args, **_kwargs: type("Result", (), {"returncode": 127})())

    result = updates.update_current_binary("0.1.3", executable_path=exe)

    assert result.updated is False
    assert result.message == "permission denied replacing binary"


def test_cli_check_updates_prints_available(monkeypatch, capsys):
    from wifit3 import __main__

    monkeypatch.setattr("sys.argv", ["wifit3", "--check-updates"])
    monkeypatch.setattr(updates, "check_for_updates", lambda _version: UpdateInfo(
        "0.1.3", "0.1.4", True, "https://example/release", True))

    with pytest.raises(SystemExit) as e:
        __main__.main()

    assert e.value.code == 0
    assert capsys.readouterr().out == (
        "current: 0.1.3\n"
        "latest: 0.1.4  https://example/release\n"
        "wifit3 0.1.4 is available: https://example/release\n")


def test_cli_check_updates_warns_when_running_from_source(monkeypatch, capsys):
    from wifit3 import __main__

    monkeypatch.setattr("sys.argv", ["wifit3", "--check-updates"])
    monkeypatch.setattr(updates, "check_for_updates", lambda _version: UpdateInfo(
        "0.1.3", "0.1.3", False, "https://example/release", True, True))

    with pytest.raises(SystemExit) as e:
        __main__.main()

    assert e.value.code == 0
    assert "warning: running from source" in capsys.readouterr().out


def test_cli_update_delegates_to_update_result(monkeypatch):
    from wifit3 import __main__, __version__

    calls = []
    monkeypatch.setattr("sys.argv", ["wifit3", "--update", "--force"])
    monkeypatch.setattr(updates, "print_update_result", lambda version, force=False, allow_elevation=True: calls.append(
        (version, force, allow_elevation)) or 7)

    with pytest.raises(SystemExit) as e:
        __main__.main()

    assert e.value.code == 7
    assert calls == [(__version__, True, True)]


def test_cli_check_updates_prints_latest_when_current(monkeypatch, capsys):
    from wifit3 import __main__

    monkeypatch.setattr("sys.argv", ["wifit3", "--check-updates"])
    monkeypatch.setattr(updates, "check_for_updates", lambda _version: UpdateInfo(
        "0.1.3", "0.1.3", False, "https://example/release", True))

    with pytest.raises(SystemExit) as e:
        __main__.main()

    assert e.value.code == 0
    assert capsys.readouterr().out == (
        "current: 0.1.3\n"
        "latest: 0.1.3  https://example/release\n")
