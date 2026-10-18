from textual.app import App

from wifit3.ui.app import WifiteApp


def test_native_windows_clipboard_skips_terminal_fallback(mocker):
    native_copy = mocker.patch("wifit3.ui.app.copy_windows_text", return_value=True)
    terminal_copy = mocker.patch.object(App, "copy_to_clipboard")

    app = WifiteApp()
    app.copy_to_clipboard("pässword")

    native_copy.assert_called_once_with("pässword")
    terminal_copy.assert_not_called()


def test_failed_native_clipboard_uses_terminal_fallback(mocker):
    mocker.patch("wifit3.ui.app.copy_windows_text", return_value=False)
    terminal_copy = mocker.patch.object(App, "copy_to_clipboard")

    app = WifiteApp()
    app.copy_to_clipboard("password")

    terminal_copy.assert_called_once_with("password")
