import subprocess

import pytest
from textual.app import App

from wifit3.ui.app import WifiteApp
from wifit3.ui.clipboard import copy_copyq_text, copy_macos_text


@pytest.fixture(autouse=True)
def copyq_mirror(mocker):
    return mocker.patch("wifit3.ui.app.copy_copyq_text", return_value=False)


def test_copyq_adds_utf8_text_from_stdin(mocker):
    mocker.patch("wifit3.ui.clipboard.shutil.which", return_value="/usr/bin/copyq")
    run = mocker.patch("wifit3.ui.clipboard.subprocess.run")

    assert copy_copyq_text("pässword")
    run.assert_called_once_with(
        ["/usr/bin/copyq", "add", "-"],
        input="pässword".encode("utf-8"),
        check=True,
        timeout=2,
    )


def test_copyq_unavailable_or_failed_is_ignored(mocker):
    which = mocker.patch("wifit3.ui.clipboard.shutil.which", return_value=None)
    run = mocker.patch("wifit3.ui.clipboard.subprocess.run")
    assert not copy_copyq_text("password")
    run.assert_not_called()

    which.return_value = "/usr/bin/copyq"
    run.side_effect = subprocess.CalledProcessError(1, "copyq")
    assert not copy_copyq_text("password")


def test_native_windows_clipboard_skips_terminal_fallback(mocker, copyq_mirror):
    native_copy = mocker.patch("wifit3.ui.app.copy_windows_text", return_value=True)
    terminal_copy = mocker.patch.object(App, "copy_to_clipboard")

    app = WifiteApp()
    app.copy_to_clipboard("pässword")

    copyq_mirror.assert_called_once_with("pässword")
    native_copy.assert_called_once_with("pässword")
    terminal_copy.assert_not_called()


def test_macos_clipboard_uses_pbcopy(mocker):
    mocker.patch("wifit3.ui.clipboard.sys.platform", "darwin")
    run = mocker.patch("wifit3.ui.clipboard.subprocess.run")

    assert copy_macos_text("pässword")
    run.assert_called_once_with(
        ["/usr/bin/pbcopy"],
        input="pässword".encode("utf-8"),
        check=True,
        timeout=2,
    )


def test_failed_native_clipboard_uses_terminal_fallback(mocker):
    mocker.patch("wifit3.ui.app.copy_windows_text", return_value=False)
    mocker.patch("wifit3.ui.app.copy_macos_text", return_value=False)
    terminal_copy = mocker.patch.object(App, "copy_to_clipboard")

    app = WifiteApp()
    app.copy_to_clipboard("password")

    terminal_copy.assert_called_once_with("password")
