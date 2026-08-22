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
from wifit3.ui.app import WifiteApp
from wifit3.ui.themes import custom_themes


def test_register_app_themes_keeps_textual_themes_available():
    app = WifiteApp()

    assert "wifit3-green-dark" in app.available_themes
    assert "textual-dark" in app.available_themes
    assert "textual-light" in app.available_themes


def test_custom_themes_defines_green_dark():
    assert len(custom_themes()) > 0


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
