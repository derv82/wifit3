import subprocess
import sys
import types

import pytest
from rich.style import Style
from rich.text import Text
from textual.theme import Theme
from textual.widgets import Static

from wifit3.ui.ansi_art import (
    THEME_BARS_PRIMARY_KEY, THEME_BARS_SECONDARY_KEY,
    THEME_TEXT_PRIMARY_KEY, THEME_TEXT_SECONDARY_KEY,
    recolor_logo
)
from wifit3.ui import system_theme
from wifit3.ui.app import WifiteApp
from wifit3.ui.system_theme import SYSTEM_THEME, preferred_theme, system_appearance_label
from wifit3.ui.themes import custom_themes


def test_register_app_themes_keeps_textual_themes_available():
    app = WifiteApp()

    assert "wifit3-green-dark" in app.available_themes
    assert "textual-dark" in app.available_themes
    assert "textual-light" in app.available_themes


def test_custom_themes_define_green_dark():
    assert {theme.name for theme in custom_themes()} == {"wifit3-green-dark"}


def test_system_theme_follows_the_detected_preference(monkeypatch):
    monkeypatch.setattr("wifit3.ui.system_theme.system_appearance", lambda: "dark")
    assert preferred_theme() == "wifit3-green-dark"
    assert system_appearance_label() == "Dark"

    monkeypatch.setattr("wifit3.ui.system_theme.system_appearance", lambda: "light")
    assert preferred_theme() == "textual-light"
    assert system_appearance_label() == "Light"

    monkeypatch.setattr("wifit3.ui.system_theme.system_appearance", lambda: None)
    assert preferred_theme() == "wifit3-green-dark"
    assert system_appearance_label() == "Unavailable"


def test_xdg_portal_appearance_takes_precedence(monkeypatch):
    monkeypatch.setattr(system_theme, "_xdg_appearance", lambda: "light")
    monkeypatch.setattr(system_theme, "_gtk_appearance", lambda: "dark")
    monkeypatch.setattr(system_theme, "_xsettings_appearance", lambda: "dark")
    monkeypatch.setattr(system_theme, "_terminal_appearance", lambda: "dark")

    assert system_theme._linux_appearance() == "light"


def test_xdg_portal_parses_the_standard_preference_values(monkeypatch):
    monkeypatch.setattr(
        system_theme, "_command_output", lambda command: "(<uint32 1>,)"
    )
    assert system_theme._xdg_appearance() == "dark"

    monkeypatch.setattr(
        system_theme, "_command_output", lambda command: "(<uint32 2>,)"
    )
    assert system_theme._xdg_appearance() == "light"


def test_xdg_portal_falls_back_from_gdbus_to_busctl(monkeypatch):
    responses = iter((None, "v u 2"))
    monkeypatch.setattr(system_theme, "_command_output", lambda command: next(responses))

    assert system_theme._xdg_appearance() == "light"


def test_gtk_theme_detection_uses_color_scheme_then_theme_name(monkeypatch):
    monkeypatch.setattr(system_theme, "_command_output", lambda command: "'prefer-dark'")
    assert system_theme._gtk_appearance() == "dark"

    responses = iter(("'default'", "'Adwaita-dark'"))
    monkeypatch.setattr(system_theme, "_command_output", lambda command: next(responses))
    assert system_theme._gtk_appearance() == "dark"


def test_xsettings_theme_detection(monkeypatch):
    monkeypatch.setattr(system_theme, "_command_output", lambda command: "Arc-Dark")

    assert system_theme._xsettings_appearance() == "dark"


def test_terminal_theme_detection_from_colorfgbg(monkeypatch):
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert system_theme._terminal_appearance() == "dark"

    monkeypatch.setenv("COLORFGBG", "0;15")
    assert system_theme._terminal_appearance() == "light"

    monkeypatch.delenv("COLORFGBG")
    assert system_theme._terminal_appearance() is None


def test_linux_system_theme_reports_unavailable_without_gsettings(monkeypatch):
    monkeypatch.setattr(system_theme.sys, "platform", "linux")
    monkeypatch.setattr(system_theme.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        FileNotFoundError()
    ))

    assert system_theme.system_appearance() is None
    assert system_theme.system_appearance_label() == "Unavailable"


def test_macos_absent_dark_appearance_means_light(monkeypatch):
    monkeypatch.setattr(system_theme.sys, "platform", "darwin")
    monkeypatch.setattr(
        system_theme.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "missing preference"),
    )

    assert system_theme.system_appearance() == "light"


def test_windows_theme_detection_reads_the_personalization_registry(monkeypatch):
    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        OpenKey=lambda hive, path: RegistryKey(),
        QueryValueEx=lambda opened_key, name: (0, None),
    )
    monkeypatch.setattr(system_theme.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", winreg)

    assert system_theme.system_appearance() == "dark"


def test_app_applies_the_system_theme(monkeypatch):
    monkeypatch.setattr("wifit3.ui.system_theme.system_appearance", lambda: "light")
    app = WifiteApp()

    app.set_theme_preference(SYSTEM_THEME)
    assert app.theme_preference == SYSTEM_THEME
    assert app.theme == "textual-light"

    monkeypatch.setattr("wifit3.ui.system_theme.system_appearance", lambda: "dark")
    app.refresh_system_theme()
    assert app.theme == "wifit3-green-dark"


def test_all_custom_themes_apply_cleanly():
    for theme in custom_themes():
        assert theme.name
        theme.to_color_system().generate()   # raises ColorParseError on any bad color


def test_recolor_logo_maps_baked_ansi_palette():
    text = Text("abcd")
    text.stylize(Style(color="#ffffff", bgcolor="#00ff00"), 0, 1)
    text.stylize(Style(color="#ffffff", bgcolor="#008000"), 1, 2)
    text.stylize(Style(color="#808080"), 2, 3)
    text.stylize(Style(color="#123456", bgcolor="#654321"), 3, 4)

    recolored = recolor_logo(text, {
        THEME_BARS_PRIMARY_KEY: "#010203",
        THEME_BARS_SECONDARY_KEY: "#040506",
        THEME_TEXT_PRIMARY_KEY: "#070809",
        THEME_TEXT_SECONDARY_KEY: "#0a0b0c",
    })

    styles = [span.style for span in recolored.spans]
    assert str(styles[0]) == "#070809 on #010203"
    assert str(styles[1]) == "#070809 on #040506"
    assert str(styles[2]) == "#0a0b0c"
    assert str(styles[3]) == "#123456 on #654321"


def test_recolor_logo_uses_dark_defaults_when_variable_is_missing():
    text = Text("x")
    text.stylize(Style(color="#ffffff", bgcolor="#00ff00"), 0, 1)

    recolored = recolor_logo(text, {THEME_TEXT_PRIMARY_KEY: "#111111"})

    assert str(recolored.spans[0].style) == "#111111 on #00ff00"


def test_recolor_logo_uses_light_defaults_for_textual_light_theme():
    text = Text("x")
    text.stylize(Style(color="#ffffff", bgcolor="#00ff00"), 0, 1)

    recolored = recolor_logo(text, {}, dark=False)

    assert str(recolored.spans[0].style) == "#111111 on #00bb00"


@pytest.mark.usefixtures("no_usb_devices")
async def test_splash_logo_uses_current_theme_variables():
    app = WifiteApp()
    app.register_theme(Theme(
        name="logo-test", primary="#ffffff",
        variables={THEME_BARS_PRIMARY_KEY: "#010203", THEME_TEXT_PRIMARY_KEY: "#040506"},
    ))
    app.theme = "logo-test"

    async with app.run_test() as pilot:
        logo = pilot.app.screen.query_one("#ascii-art", Static).content

    styles = {str(span.style) for span in logo.spans}

    assert any("#010203" in style for style in styles)
    assert any("#040506" in style for style in styles)
