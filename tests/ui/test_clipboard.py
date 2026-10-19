from textual.app import App

from wifit3.ui.app import WifiteApp
from wifit3.ui.clipboard import copy_macos_text


def test_native_windows_clipboard_skips_terminal_fallback(mocker):
    native_copy = mocker.patch("wifit3.ui.app.copy_windows_text", return_value=True)
    terminal_copy = mocker.patch.object(App, "copy_to_clipboard")

    app = WifiteApp()
    app.copy_to_clipboard("pässword")

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
