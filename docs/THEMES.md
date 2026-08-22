# Themes

Wifit3's custom themes are plain Textual `Theme` objects in
[`src/wifit3/ui/themes.py`](../src/wifit3/ui/themes.py).
The themes are registered alongside Textual's built-in themes on startup.

**RTFM:** Textual's `Theme` Reference: https://textual.textualize.io/guide/design/

## Adding a new theme

Two edits in `themes.py`:

1. Write a `def` that returns a `Theme`.
2. Add it to the return list in `custom_themes()`:

```python
def custom_themes() -> list[Theme]:
    return [
        _wifit3_green_dark(), 
        _my_theme(),   # add here
    ]
```

That is the whole wiring. Registration, the picker entry, and persistence are automatic.

## Example theme

Only `name` and `primary` are required; other colors are defaulted.

```python
# Rename the method to your theme's name
def _my_theme() -> Theme:
    return Theme(
        name="my-theme-dark",   # your theme's name: unique
        dark=True,              # False for light theme
        primary="#00ff88",
        secondary="#00c8ff",
        accent="#00ff88",
        foreground="#d8ffe8",
        background="#050805",
        success="#00ff88",
        warning="#ffd75f",
        error="#cc6666",
        surface="#0b120b",
        panel="#101810",
        variables={
            "block-cursor-background": "#00ff88",
            "block-cursor-blurred-background": "#1f5f3f",
            "block-hover-background": "#163322",
            "input-selection-background": "#005f3a",
            "screen-selection-background": "#007a48",
            # Splash's Logo (WiFi bars):
            THEME_BARS_PRIMARY_KEY: "#00ff22",
            THEME_BARS_SECONDARY_KEY: "#008f22",
            # Splash's Giant letters ("wifit3"):
            THEME_TEXT_PRIMARY_KEY: "#f4fff8",
            THEME_TEXT_SECONDARY_KEY: "#7aa88a",
        },
    )
```

Colors are `#rrggbb` hex, or `#rgb`, or named colors, whatever
is supported by Textual (*RTFM*).

A syntax error or invalid color will crash with an error message
explaining the problem.
