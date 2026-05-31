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

**RT3572 demoted to unsupported (2026-05-31).** The only test unit
(ALFA AWUS051NH v2, bought 2015) turned out to be **counterfeit with a blank
EFUSE** — so it can't validate the chipset, and TX/RX are crippled by the missing
factory RF cal. The `chips/rt2800usb/` driver itself stays supported (verified
faithful on its RT5372 + RT5572 siblings); this is the *unit*, not the port. See
"Broken / paused" below + the blank-EEPROM-override idea, and
`chips/rt2800usb/RT2800USB.md` § "RT3572 unburned-EFUSE behaviour".

**Possible cross-rtw88 2.4 GHz RX weakness (investigate).** Hardware testing
shows weaker-than-expected 2.4 GHz reception on multiple rtw88-family cards:
RTL8814AU (severe — 0.5–2 beacons/s vs ~10/s on 5 GHz), RTL8821AU (~7/s for a
close router), and RTL8822BU (a 5 GHz AP at −50 dBm but a 2.4 GHz AP at −81 dBm
at the same spot — ~31 dB, backwards from physics since 2.4 GHz should carry
*better*). All three share `chips/rtw88_base/`, so suspect a shared 2.4 GHz RX
path (band-switch RX / AGC gain) or an RSSI-calc offset rather than three
independent bugs. **Disambiguate with a Linux A/B:** run the same card on the
in-kernel driver at the same spot — normal 2.4 GHz beacon rate / RSSI there means
it's our port; also-low means hardware/environment. (5 GHz RX is healthy on all
three.) Update 2026-05-31: the RTL8188EUS (rtl8xxxu, **not** rtw88) also showed
lowish 2.4 GHz beacon rates (~6–7/s on one AP, ~1–3/s on a farther one) — weaker
evidence (different APs/channels), but if the pattern holds across families it's
less a shared `rtw88_base` bug and more a general userland-RX sensitivity question
(or just environment). The Linux A/B is the decider; ideally compare the *same*
AP. **Counter-example 2026-05-31: the RT5572 (rt2800usb) reads 2.4 GHz and 5 GHz
at the *same* power with excellent beacon rates across the band** — so weak 2.4 GHz
RX is NOT universal across userland drivers. That points back at the rtw88 family
(and possibly rtl8xxxu) specifically, not a general userland-RX limitation —
i.e. more likely our port on those families than the PyUSB approach itself.

Family-shared infrastructure (`chips/rtw88_base/`) covers transport, the
phy_cond walker, power_seq runtime, RF SIPI, TX checksum, RX-desc parser, and
legacy MCUFWDL FW upload — shared by the 88xxA (8821a/8812a), 8822b, and 8814a.

## Hardware: En-Route (check doorstep)
- Panda PAU06 - RT5372
- Alfa AWUS036NH - RT3070

## Broken / paused

- **Ralink RT3572 (ALFA AWUS051NH v2) — counterfeit test unit, demoted from
  supported (2026-05-31).** The only unit on hand is a counterfeit with a **blank
  EFUSE** (no factory RF calibration): the driver runs its normal init over
  uncalibrated silicon → forced 1T1R, rx-filter cal loop rails to a non-physical
  value, TX power stuck at the low fallback → weak, unstable TX/RX. There may not
  even be a genuine RT3572 die inside. The `chips/rt2800usb/` driver is verified
  faithful (green/partial on RT5372 + RT5572), so this is the unit, not the port.
  **To re-promote: acquire a genuine RT3572 and re-run the matrix**, OR land the
  blank-EEPROM override below (which may partially rescue *this* unit and is the
  real fix for genuine cards that ship blank). Findings kept in
  `VERIFICATION.md` § "Unsupported — pending genuine hardware" +
  `chips/rt2800usb/RT2800USB.md` § "RT3572 unburned-EFUSE behaviour".

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

## Blank-EEPROM override (rt2800usb — RT3572 rescue + no-EFUSE cards)

**Idea:** when a card's EFUSE/EEPROM reads blank, substitute a known-good
512-byte image into the *in-RAM* parsed struct so the chip is configured from
sane values instead of all-`0xFF`. Subsumes the deferred "93C66 EEPROM fallback"
item in `RT2800USB.md` — same need (no usable on-chip config), one mechanism.

**Soft override only — never burn fuses.** EFUSE is one-time-programmable (bits
blow `0→1`, permanently, vendor tooling); a wrong burn bricks the card or sets an
illegal RF/regulatory state with no undo. We do **not** write hardware. We only
replace the values the driver reads into RAM at init (`efuse.py` already parses
the EFUSE into a struct — feed it our image instead when blank). Fully reversible,
zero risk.

**Design (discuss class shape with lead before coding):** an `EepromOverride`
source in `efuse.py` — detect blank (identity programmed but RF/cal region `0xFF`,
`NIC_CONF0 == 0`), load a 512-byte image, and produce the *same* `EepromValues`
the normal parser yields. Gate behind an **explicit flag/CLI opt-in** so it never
silently fakes calibration on a healthy card — surfacing fake cal as real is worse
than a known-weak card. Image provenance: kernel `rt2800` defaults, or a dump from
a genuine RT3572 if one is ever acquired.

**Honest expectations (it's a gamble, but the feature is worth it regardless):**
- **TX should improve** — power is stuck at the low fallback (`RFCSR12=0x6b`, max
  attenuation) *because* the EFUSE reads blank; a good image with real
  `default_power` lifts it. Clear potential win.
- **Per-unit RF cal can't be faked** — crystal/freq trim + power cal are measured
  per individual card at the factory. A generic image is *better than blank* but
  has the wrong trim for this die; on counterfeit silicon those values were never
  measured and don't exist to copy.
- **RX is the open question** — the rx-filter cal is a *runtime* loopback sweep
  (RFCSR24/BBP55), not an EFUSE value, and it **rails** on this unit. Either (A)
  the blank EFUSE mis-configures the front-end earlier in init → loopback dies →
  rail (a good image might revive RX), or (B) the counterfeit front-end is just
  bad and no image helps. Unknown until tested.
- **Worst case:** counterfeit silicon doesn't respond and nothing changes. No
  software fixes fake hardware.

**Experiment:** inject a plausible image into the RAM struct on the RT3572, A/B
the beacon rate + deauth strength vs blank. Low cost, real learning; builds the
genuine-no-EFUSE-card feature either way. If it meaningfully rescues the unit,
re-run the matrix and reconsider the demotion.

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

WEP suite scoped in `src/wifit3/engine/attacks/wep/README.md`. **Open WEP TODO —
fragmentation software-sequence support:** fragments must share one 802.11
sequence number (inject with `en_hwseq=0`), exposed via `send_raw(sw_seq=)` + a
driver's `SUPPORTS_SW_SEQ`. Only `rtl8821au` implements it, so `aireplay -5` works
there and nowhere else (HW-confirmed fail on 8812au / 8814au / 8822bu / ar9271 —
see the WEP README § "Known issue"). (1) *Now, no hardware:* gate
`fragmentation.py` / `campaign.py` on `iface.supports_sw_seq` and disable/grey the
Frag button (or refuse with a clear message) instead of spinning on "seed
wouldn't relay." (2) *Real fix, per-driver + HW:* lift the 8821au
`build_tx_desc_data` (`en_hwseq=0` + `SW_SEQ`) into `rtw88_base/tx_common.py` and
set `SUPPORTS_SW_SEQ` on 8812au / 8814au / 8822bu (they already carry the W8/W9
fields) — likely fixes all three at once. (3) *AR9271:* separate HTC-firmware
investigation (no `en_hwseq` concept). Status of the rest:

- **4-way handshake capture** (via client deauth) — done; detected in Focus +
  Scanner, with Save.
  - *Open: dynamic channel re-steering.* Focus stays glued to the entry channel.
    If the AP CSA-jumps or shows stronger signal on another band, we miss it.
    QoL: periodically probe nearby channels (<100 ms each) and re-tune. Ties into
    ESSID-based targeting (one logical AP, multiple BSSIDs across bands).
- **PMKID** — done, wired into the UI, works well.
- **WPS** — *detection done; online PIN brute-force engine built, offline-proven,
  and hw-validated against the AirLink router (full PIN crack).* Detection: the parser decodes the WPS IE TLVs
  (`packet.py:_parse_wps_ie`) into `AccessPoint` fields; Scanner + Focus show 🔒.
  The Reaver/Bully-style attack lives in `engine/attacks/wps/` (own WSC registrar
  + crypto in pure Python — see its `README.md`): DH/KDF/AES core, M1–M7 state
  machine + split-PIN oracle, two-halves keyspace, kept-alive single-association
  + learned lock backoff, `.run` resume, all offline-tested (31 tests). Hardware
  probe `scripts/wps/wps_probe.py` confirmed the on-air EAP path and caught the
  FCS-in-Authenticator bug (now fixed). Full exchange hw-validated on the
  AirLink router (soft-lock: 30 reqs → 3–5 min cooldown → resume; split-PIN
  oracle correctly called first-half/second-half wrong, flipped scan direction,
  updated keys-remaining; full crack ~30 min). *Remaining:*
  - **Multi-router lock-cycle matrix.** Only the AirLink soft-lock is exercised.
    Test other behaviours: no-lock, longer cooldowns, and a hard-lock AP that
    never reopens — to drive the escape hatch below.
  - **Terminal hard-lock escape hatch.** `lock.py` already beats reaver here in
    most respects: it reads the out-of-band beacon `wps_locked` IE
    (`interface.py:254` re-reads it every beacon that carries the WPS IE),
    splits hard (beacon-advertised) vs soft (N pre-oracle strikes), and learns a
    *measured* backoff biased to the AP's real observed lock duration (vs
    reaver's flat 60s). The gap: a permanently-locked AP still loops
    lock→wait→retry forever. Add "locked across N learned-backoff cycles with
    zero progress → stop and tell the user to reboot/toggle WPS" (the
    warm-reattach "please replug" honesty pattern) instead of spinning silently.
  - **Focus WPS panel** (M8, passive-by-default behind a button).
  - **PixieWPS** (deferred — numpy/glibc dependency question to settle first).
- **WPA3 downgrade** (transition mode) — respond to probe requests.
- **Evil Twin** (2nd interface) — unproven value, low priority.

## Planned features

### Multi-card support (big swing — the "we COULD do this" idea)

Run 2+ USB cards concurrently in one wifit3 session, pooling their RX (and
splitting their TX). The point isn't that it's easy — it's that it's *possible*,
because the drivers were built generic from day one (`WlanDriver` Protocol, no
global state assumed). The substrate is already there; what's missing is making
the layer ABOVE the driver multi-instance-safe.

The vision:
- **Pooled RX.** Two cards → ~2× the beacons/EAPOL/IVs; handshake capture can
  land on whichever card hears the client. The Scanner shows the *union* of APs
  from all cards.
- **Hot-plug.** Plug in a second (even shitty) card *while wifit3 is running* and
  watch its APs merge into the live list. Unplug → its contribution drains out,
  session keeps going.
- **Coordinated channel strategy.** Split the channel set across cards (card A
  does 1–6, card B does 7–13) so each hops less and dwells longer — fewer missed
  frames. Or pin one card to a target's channel while another keeps scanning.
- **TX/RX split.** Dedicate one card to injection (deauth/replay) and another to
  pure RX, so we never miss the handshake our own deauth provoked (today the
  half-duplex radio can't TX and RX the same instant).
- **Multi-target attacks.** One card per target, attacking several APs at once.

Why it's a huge undertaking: nearly everything stateful today implicitly assumes
*one* card — channel hopping, the AP/Client registry, the per-attack campaigns,
the RX reader thread, the UI's "the interface." Multi-card means lifting all of
that into runtime-safe, multi-instance components with a coordinator above them
that merges their AP/Client views, arbitrates channel plans, and routes TX to
the right card. `WlanInterface` becomes one-per-card; a new orchestrator owns the
fleet + the merged model the UI renders. Hot-plug adds dynamic add/remove of a
card mid-session (USB enumeration watcher → spin up/tear down an interface +
its hopper without disturbing the others).

Not scoped, not soon — recorded because the architecture genuinely permits it and
it would be a standout feature. Prereq-ish: the `WlanDeviceManager` already does
generic VID:PID discovery, so multi-device *enumeration* is mostly there; the
work is everything downstream of "I have N interfaces" being singular today.

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

## Small bugs / QoL

- **Driver wedge/replug warnings don't reach the UI** — when a driver detects an
  unrecoverable USB/RX wedge it logs an actionable "please unplug + replug"
  warning, but the UI stays silent. Two cases seen:
  - *Warm-reattach (init):* `ui/screens/splash.py` raises a generic
    `RuntimeError("Hardware failed to initialize.")` / "Failed to connect to
    wlanN" instead of the driver's replug message. (RTL8822BU, 2026-05-31.)
  - *Runtime RX wedge (mid-session):* on the RTL8812AU hop-death the Scanner just
    fades targets to dark → empty list with no message; the driver logs one
    warning. Surface it as a banner. (RTL8812AU, 2026-05-31.)
  The wedge itself is an accepted Windows/WinUSB limit (the pipe can't be reset in
  userland) — only the missing UI messaging is the bug.
- **Focus-entry channel tune sometimes doesn't take (0 beacons until re-enter).**
  Entering Focus on an AP occasionally shows 0 beacons/s; exiting to Scanner and
  re-entering Focus on the same target then works (8–9/s). Confirmed cross-family
  — RT3572 (Ralink) and MT7610U (MediaTek) — so the bug is in the **shared
  Focus→stop-hop→`set_channel` path** (`wlan/interface.py` / `ui/screens/focus.py`),
  not a driver. Likely a race/ordering issue: the channel set on Focus entry is
  lost or overridden by the channel-hopper teardown, so the first tune doesn't
  stick. Repro: Focus a known AP, watch for 0 beacons, then Focus→Scanner→Focus.
- **Beacon count truncates past 10k** — `10512` renders as `0512`. Auto-size the
  BEACONS column (without breaking right-alignment).

## Idea graveyard

Considered, designed far enough to judge, and deliberately NOT built. Recorded
so we don't re-pitch them from scratch.

### MAC / OUI vendor identification — SHELVED 2026-05-30

Goal was to show a client's device class (phone / laptop / TV / PS5) and the
AP's manufacturer. Designed it end to end — a SQLite OUI→vendor DB built from
the three IEEE registries (MA-L/M/S) with longest-prefix match, atomic rebuild
on refresh, and a curated vendor→category overlay for device hints — then killed
it on the UX, not the plumbing:

- **No room to show it.** The Scanner AP/Client tables are already horizontally
  cramped; vendor strings don't fit in a cell *at all* ("Sony Interactive
  Entertainment", or even "Espressif"). A tree/sub-row under each MAC, or a
  second muted line, eats vertical space the slim clients list can't spare.
- **No icons.** Textual (rightly) has no image support, and only a handful of
  vendors have an emoji (🍎), so an icon column can't generalize. On a web /
  large-format UI this would be a muted small-font subtitle under the BSSID —
  that affordance just doesn't exist in a terminal table.
- **Vendor ≠ device type anyway.** The OUI usually identifies the Wi-Fi *module*
  maker (Intel / Murata / AzureWave / Foxconn), not the device brand; true
  phone-vs-laptop disambiguation needs 802.11 IE fingerprinting — a much larger,
  separate project.

If revived: solve the *display* first (a Focus-screen detail panel, gated behind
opt-in, is the realistic home — not the Scanner table). The build/resolver design
is sound and can be lifted wholesale. (Aside: SQLite read concurrency is fine —
the only friction was Python's per-connection thread-affinity guard, solvable
with a connection per thread; loading the whole table into a dict was overkill.)

### Configurable TX-power override — SHELVED 2026-05-19

Not building it. The silicon supports power indices above the EFUSE regulatory
caps, and userland bypasses the kernel's clamping, so a knob is technically easy
— but the per-family constants differ wildly, "max index" means different dBm
per chip, and a blanket `--tx-power N` invites real-world harm. A researcher who
genuinely needs it in an RF cage should fork; owning that choice is the point.
Ties into the PRE-RELEASE ethics/guardrails item.
