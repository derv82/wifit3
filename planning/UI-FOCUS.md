# UI Revamp — FocusView

Status: spec locked 2026-05-19. Implementation pending.

## TARGET INFO panel

- Prefix the SSID line with `ESSID: ` (every other line has a label — odd one out).
- Color the ESSID **bold cyan**. Reserves green/red for the security semantic axis; cyan reads as "identifier".
- Replace `Age: mm:ss` with **`Last Beacon: Ns ago`** (uses new `AccessPoint.last_seen`). Drop Age entirely — it was effectively static and confusing.
- Keep BSSID, Channel lines.
- **Channel line**: append `(Locked)` while in FocusView (hopper is stopped). Color it.
  - e.g. `Channel: 6 [green](Locked)[/]`

## SECURITY panel

- **Encryption line**: mirror Scanner color scheme + AKM dimming. Allow `CCMP` to appear here (detailed view).
- **PMF** colors:
  - Required → red
  - Optional / Capable → yellow
  - Disabled → dim
- **WPA3 sub-line**: hide entirely unless `ap.wpa3`. When shown:
  - `Pure WPA3-SAE` → green
  - `Transition Mode` → reuse the `WPA3→WPA2` motif

## CAPTURE panel

- Keep `Beacons: N (X.X/s)` — single-target context, the rate is meaningful here.
- **POWER** (renamed from PWR).
- Handshake line: keep current `Captured x2 (+1 partial)` format.
- PMKID line: keep current format.

## CLIENTS table

Columns: `[ ]` │ MAC │ POWER │ PKTS │ CAPTURES

- Rename `Handshake` → **CAPTURES** (folds in PMKID indicator).
- CAPTURES content:
  - Keep current `M1+M2 ✓` / `M2×3,M1` detail (per user — useful).
  - Append `+PMK` when PMKID also captured.
- POWER right-aligned.
- PKTS (was `Frames`) right-aligned.

### Selection keybindings (bug fix)

- SPACE should toggle client selection (matches `ChannelFilterDialog` in scanner).
- ENTER should also toggle — keep current behavior.
- ESCAPE → Back to Scanner — keep.

## ATTACKS panel

### Layout

Single ATTACKS panel, current 2-row × 3-col grid (Option A). Splitting into CLIENT/TARGET sub-panels was considered but rejected: forces an awkward home for Save, and the variant colors below already give visual grouping. Revisit if it feels cramped once everything else lands.

### Buttons

- `Deauth Sel` → **`Deauth [x]`** (compact, matches the noisy-ascii UI vibe).
- No icons (held off — every button is "TX inject frames", icon differentiation would be forced).
- Keep disabled placeholders for unimplemented attacks (SAE Probe, WPA3 Down) — signals roadmap.
- Variant convention (keep approximate current):
  - Deauth All = `error` (red)
  - Deauth Sel = `warning` (yellow)
  - PMKID / SAE Probe / WPA3 Down = `primary`
  - Save = `success`, enabled iff `ap.has_capture` (already wired)

## EVENT LOG

- Drop ASCII prefixes (`[+]`, `[!]`, `[✓]`).
- Replace with unicode glyphs:
  - `→` received frame (EAPOL M2 from client, etc.)
  - `✓` completion (handshake complete, PMKID harvested)
  - `⚠` warning / partial / no-result
  - `✗` failure
- Keep existing color severity: cyan=info, green=capture, red=error, yellow=warn.
- Keep timestamp prefix.

## Header strip

- Channel-locked indicator (covered above in TARGET INFO).
- Other badges (signal trend, attack-in-progress): YAGNI for now.

## Implementation order (suggested)

1. SSID labelling + Last-Beacon line (depends on Scanner's `last_seen` model field).
2. Channel-Locked indicator.
3. SECURITY panel color-coding + WPA3-conditional hide.
4. CLIENTS table rename/reorder/align.
5. Selection keybindings bug fix (SPACE).
6. EVENT LOG glyph replacement.
7. ATTACKS panel layout (after open decision resolved).
