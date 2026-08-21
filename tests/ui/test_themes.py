from pathlib import Path

import pytest
from rich.text import Text
from textual.theme import Theme
from textual.widgets import Static

from wifit3.ui.ansi_art import recolor_logo
from wifit3.ui.app import WifiteApp
from wifit3.ui.themes import (
    BUILTIN_THEMES_DIR,
    ThemeFilePoller,
    ThemeLoadError,
    load_theme_file,
    register_app_themes,
)


class _ThemeApp:
    def __init__(self):
        self.themes = {}

    def register_theme(self, theme):
        self.themes[theme.name] = theme


def test_builtin_green_theme_defines_selection_colors():
    theme = load_theme_file(BUILTIN_THEMES_DIR / "wifit3-green-dark.toml")

    assert theme.variables["block-cursor-background"] == "#00ff88"
    assert theme.variables["block-hover-background"] == "#163322"
    assert theme.variables["screen-selection-background"] == "#007a48"


def test_theme_file_loads_logo_variables(tmp_path: Path):
    path = tmp_path / "theme.toml"
    path.write_text(
        """
schema = 1

[theme]
name = "test-theme"
dark = true

[colors]
primary = "#112233"

[variables]
logo_color_primary = "#445566"
logo_color_secondary = "#778899"
logo_text_primary = "#aabbcc"
logo_text_secondary = "#ddeeff"
""".strip(),
        encoding="utf-8",
    )

    theme = load_theme_file(path)

    assert theme.name == "test-theme"
    assert theme.variables == {
        "logo_color_primary": "#445566",
        "logo_color_secondary": "#778899",
        "logo_text_primary": "#aabbcc",
        "logo_text_secondary": "#ddeeff",
    }


def test_theme_file_normalizes_compact_hex_colors(tmp_path: Path):
    path = tmp_path / "theme.toml"
    path.write_text(
        """
schema = 1

[theme]
name = "compact-theme"

[colors]
primary = "#0f"
secondary = "#abc"
accent = "#abcd"
error = "#11223344"

[variables]
logo_color_primary = "#7"
""".strip(),
        encoding="utf-8",
    )

    theme = load_theme_file(path)

    assert theme.primary == "#0f0f0f"
    assert theme.secondary == "#aabbcc"
    assert theme.accent == "#aabbcc"
    assert theme.error == "#112233"
    assert theme.variables["logo_color_primary"] == "#777777"


def test_theme_file_rejects_invalid_logo_variable_color(tmp_path: Path):
    path = tmp_path / "theme.toml"
    path.write_text(
        """
schema = 1

[theme]
name = "bad-theme"

[variables]
logo_color_primary = "not-a-color"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ThemeLoadError, match="variables.logo_color_primary"):
        load_theme_file(path)


def test_recolor_logo_maps_baked_ansi_palette():
    text = Text("abcd")
    text.stylize("#ffffff on #00ff00", 0, 1)
    text.stylize("#ffffff on #008000", 1, 2)
    text.stylize("#808080", 2, 3)
    text.stylize("#123456 on #654321", 3, 4)

    recolored = recolor_logo(text, {
        "logo_color_primary": "#010203",
        "logo_color_secondary": "#040506",
        "logo_text_primary": "#070809",
        "logo_text_secondary": "#0a0b0c",
    })

    styles = [span.style for span in recolored.spans]
    assert str(styles[0]) == "#070809 on #010203"
    assert str(styles[1]) == "#070809 on #040506"
    assert str(styles[2]) == "#0a0b0c"
    assert str(styles[3]) == "#123456 on #654321"


def test_recolor_logo_uses_dark_defaults_when_variable_is_missing():
    text = Text("x")
    text.stylize("#ffffff on #00ff00", 0, 1)

    recolored = recolor_logo(text, {"logo_text_primary": "#111111"})

    assert str(recolored.spans[0].style) == "#111111 on #00ff00"


def test_recolor_logo_uses_light_defaults_for_textual_light_theme():
    text = Text("x")
    text.stylize("#ffffff on #00ff00", 0, 1)

    recolored = recolor_logo(text, {}, dark=False)

    assert str(recolored.spans[0].style) == "#111111 on #00bb00"


@pytest.mark.usefixtures("no_usb_devices")
async def test_splash_logo_uses_current_theme_variables():
    app = WifiteApp()
    app.register_theme(Theme(
        name="logo-test", primary="#ffffff",
        variables={"logo_color_primary": "#010203", "logo_text_primary": "#040506"},
    ))
    app.theme = "logo-test"

    async with app.run_test() as pilot:
        logo = pilot.app.screen.query_one("#ascii-art", Static).content

    styles = {str(span.style) for span in logo.spans}

    assert any("#010203" in style for style in styles)
    assert any("#040506" in style for style in styles)


def test_register_app_themes_reloads_changed_user_theme(tmp_path: Path):
    path = tmp_path / "live.toml"
    path.write_text(
        """
schema = 1

[theme]
name = "live-theme"

[colors]
primary = "#111111"
""".strip(),
        encoding="utf-8",
    )
    app = _ThemeApp()
    register_app_themes(app, user_dir=tmp_path)

    path.write_text(
        """
schema = 1

[theme]
name = "live-theme"

[colors]
primary = "#222222"
""".strip(),
        encoding="utf-8",
    )

    result = register_app_themes(app, user_dir=tmp_path)

    assert result.changed is True
    assert "live-theme" in result.registered
    assert app.themes["live-theme"].primary == "#222222"


def test_register_app_themes_skips_invalid_user_theme(tmp_path: Path):
    path = tmp_path / "live.toml"
    path.write_text("not toml =", encoding="utf-8")
    app = _ThemeApp()

    result = register_app_themes(app, user_dir=tmp_path)

    assert result.changed is True
    assert result.skipped == ("live.toml",)


def test_theme_file_poller_reloads_only_after_theme_file_changes(tmp_path: Path):
    app = _ThemeApp()
    poller = ThemeFilePoller(user_dir=tmp_path)
    poller.start()

    assert poller.reload_if_changed(app).changed is False

    path = tmp_path / "live.toml"
    path.write_text(
        """
schema = 1

[theme]
name = "live-theme"
""".strip(),
        encoding="utf-8",
    )

    result = poller.reload_if_changed(app)

    assert result.changed is True
    assert result.registered == ("wifit3-green-dark", "live-theme")
    assert app.themes["live-theme"].primary == "#00ff88"
