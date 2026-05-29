# Wifit3 — Status & Next Steps

Dev-facing status. The user-facing supported-card list lives in `README.md`;
per-chip detail lives in each `chips/<chip>/<CHIP>.md`. This doc tracks what's
done at a glance and what's still open.

## Supported chipsets

All rows below are fully functional: cold + warm bring-up, channel hop,
inject + sniff, wired into the TUI. See each chip's `<CHIP>.md` for detail.

| Family | Driver | Bands |
|---|---|---|
| Atheros AR9271 | `chips/ar9271/` | 2.4 (1T1R) |
| Realtek RTL8187 | `chips/rtl8187/` | 2.4 |
| Realtek RTL8188EUS | `chips/rtl8188eus/` | 2.4 (1T1R) |
| Realtek RTL8821AU | `chips/rtl8821au/` | 2.4 / 5 |
| Realtek RTL8812AU | `chips/rtl8812au/` | 2.4 / 5 (2T2R) |
| Realtek RTL8822BU | `chips/rtl8822bu/` | 2.4 / 5 (2T2R) |
| Realtek RTL8814AU | `chips/rtw88_8814au/` | 2.4 / 5 (4T4R) |
| Mediatek MT7610U | `chips/mt76x0u/` | 2.4 / 5 (1T1R) |
| Mediatek MT7612U | `chips/mt76x2u/` | 2.4 / 5 (2T2R) |
| Ralink RT2800USB (RT5372 / RT5572) | `chips/rt2800usb/` | 2.4 / (5 on RT5572) |
| Ralink RT2500USB / RT2570 | `chips/rt2500usb/` | 2.4 |

Family-shared infrastructure (`chips/rtw88_base/`) covers transport, the
phy_cond walker, power_seq runtime, RF SIPI, TX checksum, RX-desc parser, and
legacy MCUFWDL FW upload — shared by the 88xxA (8821a/8812a), 8822b, and 8814a.

## Broken / paused

- **Ralink RT3572 (AWUS051NH v2) — TX RF-silent, paused.** All digital
  indicators say it's transmitting (TX_STA_FIFO success, counters increment,
  bulk-OUT accepted) but a known-good sniffer 5cm away sees zero deauths on-air.
  Confirmed **driver-side, not a WinUSB artifact** — reproduces on Kali with the
  kernel modules unloaded. Next step is purely offline: diff the Phase-A (kernel
  `rt2800usb`, working) vs Phase-B (wifit3, failing) usbmon pcaps in
  `usb_dumps/captures_rt3572_tx_diff/` for the missing/wrong register write that
  keeps the analog stage silent. No further hardware needed.

- **Mediatek MT7921AU (AWUS036AXML) — paused, possibly a Linux dead-end.** The
  unit **never enumerated on Kali** (USB-2, USB-3, powered hub — no iface, no
  `phy`), so our airmon+usbmon ground-truth method is unavailable. It *does*
  enumerate under WinUSB on Windows (FW upload gets partway). Driver blocker is
  the **FW_START_REQ wall** (reproduces on Kali too). Leading hypothesis:
  shallow bulk-IN URB pool — the kernel pre-submits 128 URBs/endpoint before any
  FW traffic; our transport does one-at-a-time sync reads. Fix would mean a
  libusb async URB port (`libusb_submit_transfer`, pre-submit ~32 URBs/EP). First
  re-confirm the hardware enumerates at all before sinking more time in. See
  `chips/mt7921au/MT7921AU.md` + `chips/mt7921au/KALI-HANDOFF-2026-05-19.md`.

## Open follow-ups (RTL8812AU)

1. **Queue-clear bisect** — `REG_RQPN / REG_RQPN_NPQ / REG_TXDMA_PQ_MAP` get
   silently cleared in `post_mac_init_phy`; worked around via `_arm_tx_queues`
   before each `inject_frame`. Bisect diagnostic landed (`2c7a465`) — one
   `--debug` run shows which step clears them. Find root cause, drop the workaround.
2. **EFUSE-read verification** — read landed (`71699d7`); awaiting hw test to
   confirm values are readable on the AWUS036ACH and that feeding them into
   bring-up fixes the earlier "only sees the nearest/strongest AP" sensitivity gap.

## Bringing up the next card

Recipe when fresh cold-boot captures land in `usb_dumps/captures_<driver>/`
(`capture-N.pcap` + `capture-N_logs/main.log`):

1. `pcap_slicer.py <main.log> <pcap>` — map "plug-in → FW load → channel hop →
   packets" to frame ranges. Pick the cold-boot capture.
2. Pull pristine kernel source into `data_dumps/<driver>-source-v6.18/` (matches
   Kali's runtime kernel, keeps `[SRC]` cites version-aligned).
3. Extract the FW blob from the cold-boot pcap, byte-verify against
   `linux-firmware/`, ship it in `chips/<driver>/assets/`.
4. M1 = FW upload + FW_READY ACK only. Demoable, no PHY init.

**Scope: 20 MHz primary channel only.** Don't port the kernel's 40/80 MHz
channel-width path (`bw=1/2`, the `ch_group_index` offset math, the
secondary-channel + per-width `EXT_CCA`-group setup). wifit3 only ever tunes the
20 MHz primary — every frame it captures (beacons, EAPOL, WEP IVs) and transmits
(deauth/replay) rides the primary at legacy rates, so 40/80 buys nothing and is
pure port surface. (See `chips/mt76x2u/MT76X2U.md` → "Channel width — 20 MHz only".)

### Distant-future hardware ($$$)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU).
- AWUS036NH (RT3070) — should slot into `chips/rt2800usb/` as a `DeviceID` +
  chip-id extras entry + minor RXWI/TXWI tweaks, not a from-scratch port.
- Generic MT7601U — cheapest dongle, weird packet injection.

## Attack stack

WEP suite scoped in `src/wifit3/engine/attacks/wep/README.md`. Status of the rest:

- **4-way handshake capture** (via client deauth) — done; detected in Focus +
  Scanner, with Save.
  - *Open: dynamic channel re-steering.* Focus stays glued to the entry channel.
    If the AP CSA-jumps or shows stronger signal on another band, we miss it.
    QoL: periodically probe nearby channels (<100 ms each) and re-tune. Ties into
    ESSID-based targeting (one logical AP, multiple BSSIDs across bands).
- **PMKID** — done, wired into the UI, works well.
- **WPS** — *detection done; online PIN brute-force engine built + offline-proven,
  hardware-validation pending.* Detection: the parser decodes the WPS IE TLVs
  (`packet.py:_parse_wps_ie`) into `AccessPoint` fields; Scanner + Focus show 🔒.
  The Reaver/Bully-style attack lives in `engine/attacks/wps/` (own WSC registrar
  + crypto in pure Python — see its `README.md`): DH/KDF/AES core, M1–M7 state
  machine + split-PIN oracle, two-halves keyspace, kept-alive single-association
  + learned lock backoff, `.run` resume, all offline-tested (31 tests). Hardware
  probe `scripts/wps/wps_probe.py` confirmed the on-air EAP path and caught the
  FCS-in-Authenticator bug (now fixed). *Remaining:* re-validate the full
  exchange on hardware (the AirLink box was likely WPS-locked last run), wire a
  Focus WPS panel (M8, passive-by-default behind a button), and PixieWPS
  (deferred — numpy/glibc dependency question to settle first).
- **WPA3 downgrade** (transition mode) — respond to probe requests.
- **WPA3 SAE crackable groups** (19, 20, 22-24) — enumeration added; numbers not
  yet verified accurate.
- **Evil Twin** (2nd interface) — unproven value, low priority.

## Planned features

### MAC vendor identification

OUI (first 3 bytes) → vendor name. wifite2 has a fetcher
(`tools/fetch_oui.py`). Show a "Manufacturer" column in Scanner (ASUS, Apple,
…) and optionally per-client in Focus with icons. Storage: see below — but a
flat lookup table may be lighter than SQLite for this one.

### User persistence + decloak DB

A shared storage layer for three concerns (likely a full session of work):

1. **Persistent config** — theme (hardcoded `textual-dark` in `ui/app.py`),
   Scanner sort column/direction, `hashcat` path, capture output dir, default
   channel filter.
2. **Decloaked SSID DB** — when a hidden AP is decloaked via a probe response,
   we log it then lose it on exit. Persist `bssid → ssid` with `first_seen`,
   `last_seen`, `sighting_count`, `confidence` (0..1), `sources` bitmask. The
   confidence counter defends against MDK3-style probe-response spam: rare
   mappings score low, consistent ones high, conflicting evidence decays. UI:
   render stored SSID as a muted `(decloaked)` suffix for high-confidence, `?`
   for low.
3. **Storage** — SQLite (tables: `config`, `decloaked_ssids`, future
   `oui_overrides`), `platformdirs` for location (`~/.config/wifit3/` /
   `%APPDATA%/wifit3/`), `PRAGMA user_version` from day one. Privacy: the
   decloak DB is a passive-sniffing artifact — note it in the ethics checklist.

   *Open:* config in TOML (human-editable) + decloak in SQLite? Auto-prune old
   decloak entries vs grow forever? (DB is per-machine, doesn't roam.)

### Live packet dashboard (Focus top-right)

Cosmetic-but-cool. The Focus screen has an unused panel above the EVENT LOG /
right of CAPTURE that stands out on ultra-wide terminals. Idea: turn it into a
colour-coded packet-class meter for the focused AP — htop / Windows Task
Manager-style columnar bars. Every ~3 s, plot what we saw in the previous
window across a row of category bars, height-scaled by volume:

  beacons (blue) · data (green) · injected (orange) · deauths (red) ·
  WPS / EAPOL (pink) · …

Cells are additive within a window (8 beacons + 4 data + 1 inject in a 3-s
slice → 2 full blue cells + 1 full green + 1 quarter orange). Constraints to
fit a ~50×8 area without a border:

  - granularity is coarse (no per-frame ticks; one column per window).
  - aggregate by class, not by frame — overflow saturates to "full cell".
  - empty windows compress to a single line of dim dots so the rest of the
    panel doesn't shift.

Textual can do this with `RichLog`-style append + a per-class colour palette,
or a small custom widget that paints unicode block characters. Most of the
data is already passing through the parser + injector — wire a counter into
each path and sample it on a 3 s timer. Low risk (read-only on the wire), high
"feels alive" value when the radio is actually doing something.

### Configurable TX-power override — SHELVED 2026-05-19

Not building it. The silicon supports power indices above the EFUSE regulatory
caps, and userland bypasses the kernel's clamping, so a knob is technically easy
— but the per-family constants differ wildly, "max index" means different dBm
per chip, and a blanket `--tx-power N` invites real-world harm. A researcher who
genuinely needs it in an RF cage should fork; owning that choice is the point.
Ties into the PRE-RELEASE ethics/guardrails item.

## Small bugs / QoL

- **Beacon count truncates past 10k** — `10512` renders as `0512`. Auto-size the
  BEACONS column (without breaking right-alignment).
