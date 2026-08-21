# Themes

Wifit3 can register additional Textual themes from TOML files.

- Built-in Wifit3 themes live in `src/wifit3/ui/themes/`.
- User themes live in the platform config directory under `themes/`.
  - Linux example: `~/.config/wifit3/themes/`

Themes are registered before the saved `theme` preference is applied, so a config value can point at a user-defined theme name. During theme development, run Wifit3 with `--theme-reload` to reload changed theme files while the TUI is running.

## Schema

```toml
schema = 1

[theme]
name = "wifit3-green"
display_name = "Wifit3 Green"
dark = true

[colors]
primary = "#00ff88"
secondary = "#00c8ff"
accent = "#00ff88"
foreground = "#d8ffe8"
background = "#050805"
surface = "#0b120b"
panel = "#101810"
success = "#00ff88"
warning = "#ffd75f"
error = "#ff5f5f"

[variables]
logo_color_primary = "#00ff88"
logo_color_secondary = "#008f55"
logo_text_primary = "#f4fff8"
logo_text_secondary = "#7aa88a"
```

## Required fields

```toml
schema = 1

[theme]
name = "my-theme"
```

`[theme].dark` is optional and defaults to `true`.

## Supported color keys

The `[colors]` table accepts:

```text
primary
secondary
warning
error
success
accent
foreground
background
surface
panel
boost
```

`primary` defaults to Wifit3 green (`#00ff88`) when omitted. Other missing colors are left for Textual to derive/default.

Theme colors may use Rich/Textual color names or hex values. Wifit3 also normalizes compact hex forms before handing them to Textual:

```text
#x        -> #xxxxxx          grayscale nibble
#xx       -> #xxxxxx          grayscale byte
#rgb      -> #rrggbb          CSS shorthand
#rgba     -> #rrggbb          CSS shorthand; alpha ignored
#rrggbbaa -> #rrggbb          alpha ignored
```

Examples: `#0f` becomes `#0f0f0f`, `#abc` becomes `#aabbcc`, and `#abcd` becomes `#aabbcc`.

## Logo colors

Splash logo ANSI art has four replaceable palette slots. Define them in `[variables]`:

```toml
[variables]
logo_color_primary = "#00ff88"
logo_color_secondary = "#008f55"
logo_text_primary = "#f4fff8"
logo_text_secondary = "#7aa88a"
```

Missing logo variables fall back to Wifit3's default logo palette for the active Textual theme mode: dark themes keep the original bright green logo, while light themes use darker ink-friendly logo colors.

## Loader behavior

- Unknown keys are ignored.
- Invalid TOML files are skipped and logged.
- Invalid color strings skip that theme.
- User themes are registered after built-in Wifit3 themes, so a user theme can override a Wifit3 theme name intentionally.
- Built-in Textual themes are not modified by Wifit3.

## Live reload

Live theme reload is opt-in so normal runs keep the dependency/runtime surface minimal:

```bash
uv run wifit3 --theme-reload
```

With `--theme-reload`, the app checks built-in and user theme `.toml` files once per second. When a file changes, Wifit3 re-registers the available themes and re-applies the active theme, so color edits should show up without restarting the TUI.

If a file is invalid while you are editing it, Wifit3 keeps running, skips that file, and shows a warning toast. Save a valid TOML file again and it will be picked up on the next reload tick.
