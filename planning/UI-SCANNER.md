# UI Revamp — ScannerView

Status: spec locked 2026-05-19. Implementation pending.

## Column layout

Left → right:

```
BSSID │ CH │ POWER │ BEACONS │ #CLI │ ENCRYPT │ SSID
```

- **BSSID** — unchanged.
- **CH** — right-aligned.
- **POWER** — was `PWR`. Right-aligned. `"{n} dBm"`.
- **BEACONS** — integer only, right-aligned. Drop the `"(N/s)"` rate suffix.
- **#CLI** — count of `iface.clients` with `bssid == ap.bssid`, excluding `iface.forged_macs`. Empty cell if 0.
- **ENCRYPT** — see color/format spec below. Was `ENC`.
- **SSID** — last so long names can flow wide.

## Sort cadence

- Cell values continue to update at 60 FPS in `refresh_table`.
- `_apply_sort()` moves to its own `set_interval`, ~2 s, so rows stop bouncing.
- Default sort unchanged: POWER, descending.

## Stale-row dim-out

- Add `last_seen: float` to `AccessPoint`.
- `WlanInterface._on_frame_parsed` (`interface.py`) bumps `last_seen` on every frame from that BSSID.
- ScannerView dims the entire row when `time() - last_seen > 15 s`.
- No removal — just dim. Channel-filter pruning still removes outright.

## Encryption color/format

| Label          | Color                                             |
|----------------|---------------------------------------------------|
| OPEN           | dim                                               |
| WEP            | red                                               |
| WPA (legacy)   | red                                               |
| WPA2           | green                                             |
| WPA3           | red                                               |
| WPA3 (Trans)   | `[red]WPA3[/]→[green]WPA2[/]`                     |
| OWE            | yellow                                            |

AKM/cipher in dim parens, CCMP dropped:

- `[green]WPA2[/] [dim](PSK)[/]`
- `[red]WPA3[/] [dim](SAE)[/]`
- `[red]WPA3[/]→[green]WPA2[/] [dim](PSK+SAE)[/]`

### Model changes

`AccessPoint` (`src/wifit3/engine/models.py`):

- `akms: list[str] = Field(default_factory=list)`
- `pairwise_cipher: Optional[str] = None`
- `last_seen: float = Field(default_factory=lambda: time.time())`

`packet.py` already computes `akms` + `pairwise_cipher` — wire them through `interface.py` so they hit the model. The view builds markup from structured fields; `ap.encryption` becomes a render-side concern (still useful for saved captures / non-UI consumers).

## SSID column

- White italic.
- `<Hidden>` rendered dim.
- Capture markers inline after SSID:
  - `[green]✓HS[/]` if any handshake complete on this AP.
  - `[green]✓PMK[/]` if any PMKID captured.
  - Both if both. Neither shown if neither — no empty slots.

## Logging

- Decloak: already wired (`scanner.py:117-120`). Keep.
- Add capture-event logging (handshake complete, PMKID harvested) to ScannerView. Factor `_poll_capture_events` out of FocusView into a shared helper consumed by both views.

## Theme colors

- Hard-code rich-named colors for now (`green`, `red`, `dim`, `yellow`).
- Theme-var resolution (`$success`/`$error`/etc.) deferred — YAGNI until it actually clashes with a theme in use.

## Implementation order (suggested)

1. Model fields (`akms`, `pairwise_cipher`, `last_seen`) + wire through `interface.py`.
2. Column rename/reorder + alignment + drop-rate-suffix.
3. New `#CLI` column.
4. ENCRYPT color/format helper (pure function, easy to unit-test).
5. SSID styling + capture markers.
6. Stale-row dim-out.
7. Decoupled sort interval.
8. Shared capture-event helper + ScannerView wiring.
