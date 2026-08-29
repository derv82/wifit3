# Preferences modal (app-level config)

Status: spec agreed, not built. Scratch doc.

`Ctrl+P` opens a hand-rolled `PreferencesModal` for app-level user config. (`p` alone is
taken, e.g. PMKID in Focus.) We override Textual's built-in command palette, which we only
kept for its theme chooser; the modal's Theme `Select` replaces that.

Wiring: `ENABLE_COMMAND_PALETTE = False` on `WifiteApp`, plus `Binding("ctrl+p", "preferences")`.

## Design decisions (settled)

1. **No generic pref schema.** We rejected a `Pref` / `get_ui_prefs()` / `set_ui_prefs()`
   abstraction. It would be gutted the moment a non-simple setting arrives (the cracker
   exe/args/format block). Adding a preference is a deliberate UX decision, not a one-liner.

2. **`Config` stays dumb.** It is a plain data holder: class-attr defaults + `load()` /
   `save()`. It has no UI awareness. A new field costs three lines: the class attr default,
   a `load()` line, a `save()` line. `_fmt` already covers bool / int / float / list / str.

3. **The modal owns all UI + config-structuring.** It reads `Config.<key>` when composing
   widgets and writes `Config.<key> = value` per field on save, then calls `Config.save()`.
   Every setting is wired by hand. No loop over a schema.

4. **Preferences is a curated SUBSET of Config.** Persisted != preference. `scanner_sort`,
   `scanner_sort_reverse`, and other persisted UI state do NOT appear in the modal. Only the
   fields the modal explicitly composes show up.

## The pattern

```python
class Config:                      # dumb data holder; load()/save() cover ALL persisted state
    captures_dir: str = "./captures/"
    save_pcap: bool = True
    crack_format: str = "hc22000"  # "pcap" | "hc22000"
    crack_exe: str = ""
    crack_args: str = ""           # $f -> the capture file of the chosen format
    # existing theme / scanner_sort / silenced_bssids unchanged

class PreferencesModal(ModalScreen):
    """Ctrl+P. Every setting wired by hand (widget on compose, Config write on save)."""
    def compose(self) -> ComposeResult:
        with Vertical(id="prefs"):
            yield Select(THEME_OPTIONS, value=Config.theme, id="theme")
            yield Input(Config.captures_dir, id="captures_dir")
            yield Checkbox("Save .pcap handshakes", value=Config.save_pcap, id="save_pcap")
            yield Select([("pcap","pcap"),("hc22000","hc22000")],
                         value=Config.crack_format, id="crack_format")
            yield Input(Config.crack_exe, id="crack_exe", placeholder="path to cracker")
            yield Input(Config.crack_args, id="crack_args", placeholder="args ($f = capture file)")
            # ... Save / Cancel ...

    def action_save(self) -> None:            # explicit, per-field. no loop.
        Config.theme        = self.query_one("#theme", Select).value
        Config.captures_dir = self.query_one("#captures_dir", Input).value
        Config.save_pcap    = self.query_one("#save_pcap", Checkbox).value
        Config.crack_format = self.query_one("#crack_format", Select).value
        Config.crack_exe    = self.query_one("#crack_exe", Input).value
        Config.crack_args   = self.query_one("#crack_args", Input).value
        Config.save()
        self.dismiss(True)
```

## v1 candidate settings

Keep the bar high. Every entry earns its place.

- Theme: dropdown of Textual themes. `Config.theme` already persists.
- Captures dir: text field, default `./captures/`.
- Save `.pcap` handshakes: checkbox. Off skips the pcap (a lever on the #14 capture-spam problem:
  write only what the user wants). We always write `.hc22000`.
- Cracker block (deferred to a second pass, see the knot below):
  - `crack_format`: choice of `pcap` | `hc22000`.
  - `crack_exe`: path to the cracker binary.
  - `crack_args`: arg string, `$f` substituted with the capture file of the chosen format.

Open: is **silenced BSSIDs** a modal entry or managed only by the Focus `s`-toggle? Undecided.

## Open UX knot: the cracker format <-> exe coupling

`crack_format` and `crack_exe` are coupled. `.hc22000` only feeds hashcat; `.pcap` feeds
aircrack and others. So the format choice constrains which exe is valid. Options, undecided:
(a) free text, trust the user; (b) `crack_format` reactively hints/validates the exe; (c) flip
it, pick the tool first and derive the format. Decide this when we build the cracker row.

## Motivating problem: capture spam (issue #14)

The handshake / PMKID capture flow spams (issue #14). The `save_pcap` toggle above is one lever
(write only what the user wants). Full solution still needs more thought; see the #14 planning.
