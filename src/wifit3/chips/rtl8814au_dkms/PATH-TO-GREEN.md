# RTL8814AU_DKMS — Path to Green (autonomous agent runbook)

> **NEXT AGENT: READ THIS FIRST. You are autonomous for the entire session.**
> Do **not** stop to report partial findings. Do **not** ask the user questions. Do **not**
> hand back after finding "3 things." Drive to GREEN, or until the *entire* init+tune path is
> audited and every divergence fixed. Commit as you go. The user is away all day — they want to
> come back to progress, not a question.

## The one definition of GREEN (non-negotiable)

GREEN = the port sustains **≥ 8 beacons/sec from the fixed 2.4 GHz reference AP** over a 60 s soak:

```
uv run python scripts/diag/beacon_watch.py --bssid <ref> --channel 1 --duration 60
```

- The reference AP (2.4 GHz ch 1; and a 5 GHz ch 36 one) is defined by SSID+BSSID in your loaded
  memory **`feedback_beacon_rate_bar.md`** — read the BSSID there and pin it with `--bssid`.
- A known-good MT7921AU holds **~9.6/s flat, #1-ranked** at this AP, same spot. That is the bar.
- **< 8/s is NOT GREEN — period.** `verify_pcap` passing, an "audit passed", a high total-AP
  count, "RX healthy", "IGI adapting" — **none of these are green.** Only the reference-AP rate is.
- Today this port hears the reference AP **worst of the room (~2.6–3/s)** while the *aggregate*
  looks fine (~26–32/s). **Aggregate / best-AP / total-AP-count are lies. Ignore them entirely.**

## Step 0 — BASELINE FIRST (before you read a line of code)

Get two numbers in hand so the gap is concrete and you have a falsifiable target. Record both in
the coverage ledger.

1. **Our port, live** (the failure baseline):
   ```
   uv run python scripts/diag/beacon_watch.py --bssid <ref> --channel 1 --duration 60
   ```
   `<ref>` = the 2.4 GHz reference BSSID from your loaded memory `feedback_beacon_rate_bar.md`.
   The card was just plugged in, so this first run is **cold**. Expect ~2.6–3/s, reference AP
   ranked near-last of the room.

2. **The kernel, from the capture** (the answer-key target — no hardware, no replug):
   ```
   uv run python scripts/diag/beacon_watch_usbcap.py usb_dumps_new/captures_rtl8814au/capture-1.pcap --bssid <ref>
   ```
   `capture-1.pcap` is a **usbmon** dump, not over-the-air — `beacon_watch_usbcap.py` recovers
   beacons from the bulk-IN payloads and auto-clips to the 15 s FIXED-CH1 window. **Do NOT use
   `beacon_watch.py --pcap` here** (it expects an OTA pcap and finds nothing; the `airodump-*.cap`
   in `_logs/` is useless too — it dedups to one beacon per AP).
   **Measured target (verified): kernel = 8.7/s, median 9, 89% reception, #1-ranked, bulk-IN ep
   0x81.** Same driver, same AP, same USB bus. Our live port gets ~2.6–3/s on that AP → a ~3× gap
   that is 100% ours.

You are chasing #1 up to #2. **Re-run #1 (live) after every fix** — it is the only signal that
counts. (Live runs after the first are warm; the offline `--pcap` state-diff is your
replug-independent primary tool.)

## What is already established — do NOT re-investigate these (all disproven)

- **NOT the environment** — the known-good card gets 9.6/s in the same spot, same minute.
- **NOT the kernel** — the kernel driver gets 8–10/s on this exact card in the capture.
- **NOT userland / WinUSB / PyUSB / the transport** — our **8821au + 8812au** ports get 8–10/s on
  the *identical* stack. They are the control group. The stack is proven.
- **NOT the hardware** — the kernel proves the silicon does it.
- **NOT the runtime watchdog / DIG loop.** The failure is present **from second one** — a 2 s
  adaptation loop cannot make a card that starts at ~1/s. The prior "severe audit" spent itself
  porting the watchdog. **That was the wrong direction. Do not repeat it.**

**Therefore the bug is in OUR `rtl8814au_dkms` init / channel-tune code.** It causes RX front-end
**saturation** (the chip's `phy_status` pwdb rails to 253 = gain set too high). It is a wrong
value, or a missing / mis-ordered write, in the bring-up. **Full stop. Stop looking for an
external cause — if you catch yourself reaching for one, you are wrong, return here.**

## Why the gate never caught it (the blind spot you MUST beat)

`scripts/rtl8814au_dkms/verify_pcap.py` feeds the port handlers the **recorded reads**
(`ReplayTransport`). So it only proves "given capture-1's reads, we emit capture-1's writes." It is
**green by construction for any value we hardcoded or mis-computed** that happens to match
capture-1. It **cannot** validate an RX-gain / AGC / efuse-derived value that is wrong on live
silicon. **`verify_pcap` green is necessary, not sufficient. The bug lives exactly in its blind spot.**

## The method (this is how you stop needing the user to babysit you)

For **every** register write and **every** branch in the vendor init+tune path, *you* answer
"does this affect RX gain / AGC / RF calibration?" by **reading the vendor source** — never by
trusting a comment. Treat every `# skip`, `# no-op`, `# RX-irrelevant`, `# validated`, `# always X`
in our port **and in `SEVERE-AUDIT.md`** as a **hypothesis to falsify** against `[SRC]` + live
silicon. **Default-assume-wrong.** (This is the comment-blind audit in `planning/PORTING.md` →
"Green ≠ faithful". Read it.)

Run **two** diffs together:

1. **Wire diff (offline, no hardware):** `verify_pcap.py` — confirm every init+tune write still
   matches the capture (no regressions). Necessary, not sufficient.
2. **State diff (this is the one that finds the bug):** the capture is the **answer key.** For
   every register the kernel writes during init+tune — *especially* RX-gain / AGC / RF / LNA /
   RFE / IGI and anything efuse-derived — **read that same register off our live chip after our
   bring-up** and confirm it holds the kernel's value. **Any divergence in a gain/AGC/RF-cal
   register is the bug or a prime suspect.** Also read the **real efuse off the live chip** and
   confirm every efuse-derived value we compute matches what the kernel computed from the same
   bytes — **never hardcoded.** (`scripts/rtl8814au_dkms/verify_efuse_pcap.py` is a starting point;
   extend it into a full live-register-vs-capture state diff.)

## Audit scope — EXHAUSTIVE (do not stop early)

Walk the **complete** init+tune path in vendor source order. Classify **every** register write and
**every** efuse field: `faithful / hardcoded / omitted / wrong-value / N-A`, each with a `[SRC]`
cite. **Maintain a coverage ledger in this file.** You are not done auditing until **100 %** of the
init+tune path is classified. Do **not** report "found 3 things" after auditing 1 %.

Port handlers to walk (`src/wifit3/chips/rtl8814au_dkms/`):
`efuse.read_chip_params` · `firmware.bring_up` · `mac.phy_mac_config` / `mac_init_misc` /
`hal_init_turn_on` · **`bb.phy_bb_config` (AGC tables — PRIME)** · **`rf.phy_rf_config` (RF gain —
PRIME)** · **`chan.init_tune` / `set_channel_bw` / `set_rfe_reg_init` (tune + RFE — PRIME)** ·
**`dm.init_hal_dm` (IGI / AGC seed — PRIME)**.

Vendor source: `usb_dumps_new/captures_rtl8814au/driver-source/` (morrownr 8814au). **Mainline
(`chips/rtw88_8814au/`, `chips/rtw88_base/`) is OFF-LIMITS — cleanroom; reading it produces a hybrid.**
Capture answer-key: `usb_dumps_new/captures_rtl8814au/capture-1.pcap` (dev 51, runs to frame 85279).

## Prime suspects (starting leads — VERIFY against source + live state, never assume)

Saturation = RX gain too high at init. Hunt the init gain chain:
- **AGC tables** in `bb.phy_bb_config` — wrong table, truncated, or wrong `rfe_type` branch.
- **RF gain / LNA** registers in `rf.phy_rf_config`.
- **RFE / antenna** config (`set_rfe_reg_init`) — 4T4R path/antenna setup (the 8822bu had an
  antenna-mux RX bug; the 4T4R analog may bite here).
- **IGI / initial-gain seed** in `dm.init_hal_dm` — seeded too high or hardcoded.
- Any **efuse-derived gain/power** value hardcoded instead of read, or mis-applied.

These are leads, not the answer. The bug may be one of them or elsewhere in init/tune — **audit all
of it.**

## The loop — iterate until GREEN

1. **Measure** — `beacon_watch --bssid <ref> --channel 1 --duration 60`. Record the rate.
   (Confirm the < 8/s failure first, so you have a falsifiable baseline.)
2. **Audit** the next un-audited region of init/tune, comment-blind: source + wire diff + **live
   register state diff vs the capture.**
3. **Fix** every divergence found, faithful to the vendor source.
4. **`verify_pcap` must still pass** (no wire regression).
5. **Re-measure.** ≥ 8/s on the reference AP → **GREEN** → document root cause here +
   `RTL8814AU_DKMS.md`, update `VERIFICATION.md` (reference-AP rate), commit. Done.
6. **< 8/s → go to 2.** Keep going until green, or until the *entire* init/tune path is audited,
   every divergence fixed, and you have documented precisely what remains.

**Commit each fix with a clear message. Do not stop to report intermediate findings.**

## Autonomy & safety

- Card (AWUS1900, `0bda:8813`) is plugged in + WinUSB-bound. **Run hardware freely:** bring-up,
  channel tune, RX, `beacon_watch`, `scan_hw`, soak. You cannot replug (physical) — do software
  bring-up each run; the **offline capture state-diff is your hardware-independent primary tool.**
- **The ONE thing you may NEVER do: live 802.11 TX — injection / deauth / any frame transmit.**
  That is the user's hands only. Everything else: go.
- Always `uv run python ...` (project deps). Never `ruff format`. Stage only your task's files.
- **No real SSID/BSSID in any committed file or commit message** (get the reference from memory).
- Report to the user only when **GREEN**, or when **genuinely, provably blocked** — and then with
  the full coverage ledger, not a 1 % sample.

## When green: do 8822bu next

Same saturation symptom, same method. Once 8814au is green, apply this runbook to `rtl8822bu_dkms`.

---

## Coverage ledger (fill as you audit — every init/tune write classified)

| Handler / region | [SRC] | wire-diff | live-state-diff vs capture | verdict |
|---|---|---|---|---|
| _(start here — efuse → turn-on → tune; classify every write)_ | | | | |
