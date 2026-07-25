# Sweep Redux — detailed per-card hardware findings

The numbers behind the `VERIFICATION.md` grades. `VERIFICATION.md` keeps one summarized row per
capability; this file keeps the raw figures those rows summarize — enough detail to make a grade
call ("5 PIN attempts, 4 reached M7, 1 timeout, latencies 2.5/3.5/8.9 s", not "WPS GOOD").

**Scope this pass** (Linux/Kali, agent-run): RX baseline A/B · TX + ACKs (ack_lab) · WPS (wps_lab)
· driver-health. **Deferred:** 20-min stress soaks (own session) · WEP / PMKID / Handshake (need the
user logged into the test router). Reference APs: **mud2g** (2.4 GHz) and **mud** (5 GHz) — no BSSIDs
in this doc.

## What each row records
- **RX / Baseline A/B** — `baseline-wifit3` vs `baseline-linux` (`driver_health` diff), reference AP
  pinned: beacons/sec wifit3 vs linux (matched window, % of linux), breadth (APs, 2.4 & 5 GHz),
  RSSI median delta, channel-tune (n/n heard their own beacon).
- **driver-health** — `driver_health.py` summary (RX health / any anomalies surfaced).
- **ACKs** — two binary axes, each **cross-checked against `planning/ACK-DRIVER-REDESIGN.md`**:
  - *HW auto-ACK* (`rx_autoack.py`): ACKs/100 to a spoofed MAC (active-monitor on), controls ~0.
  - *HW ACK-retry* (`tx_retries.py`): median on-air copies/inject — real AP (stops on ACK) vs dead
    target (piles to the retry limit), active-monitor off/on.
- **WPS** (`wps_lab.py --mode timing`, campaign-faithful knobs `--auto-ack --ack-resend
  --ack-resends 1`): attempts, assoc n/N, reach histogram (M1/M3/M5/M7), timeouts, assoc + per-stage
  latency (min/med/max), successes. Optionally `--mode campaign` for a full end-to-end run.

Legend as `VERIFICATION.md`: ✅ works · ⚠️ caveat · ❌ broken · ⬜ not run.

---

## Process notes (meta) — running log

### 2026-07-24 — pre-sweep prep + script validation (agent, no user present)
Cards plugged (all 3 wifit3-bound): MT7921AU (AWUS036AXML), RTL8812AU (AWUS036ACH),
RTL8814AU (AWUS1900). Validated the diag/lab scripts on hardware before the real sweep.

**Scripts validated live (kali user, no sudo needed for Realtek):**
- `beacon_watch --card 8812` — RX + card-select OK (301 beacons/8 s, ~6.5/s best AP).
- `baseline-wifit3 --card 8812 --chip rtl8812audkms` — full A/B pipeline OK: reference-AP
  pinning (both bands), reversed-order diff fired, old json archived to `history/`.
- `rx_autoack --test-card 8814 --prober-card 8812` — 30/30 spoofed AM-on, 0/0 controls;
  matches ACK-DRIVER-REDESIGN (8814 SPOOFABLE).
- `tx_retries --inject-card 8814 --sniffer-card 8812 --target <AirLink>` — real AP median 1
  copy (stops on ACK), dead target ~13; matches ACK-DRIVER-REDESIGN.
- `wps_lab --mode timing --card 8812 --auto-ack --ack-resend --ack-resends 1` — 3/3 M7 after
  a repair (below). Rich per-stage latency is exactly the WPS-row detail we want.

**Bugs found + fixed this session:**
- `wps_lab` / `wps_probe` had bit-rotted vs the WlanInterface/WlanArray/manager split — crashed
  on the first PIN attempt (`iface.get_access_points`, `_mgr.interfaces`, `WpsCampaign(iface,…)`).
  Repaired (commit 27275a19); campaign mode wired to a real `WlanArray` too.
- `baseline-wifit3` had no `--chip` override, so same-chipset cards clobbered one file. Added
  `--chip` (matches the `mt7921au_axml` / `rt5372_pau06` slug convention already in `history/`).

**Open issues for the real sweep:**
- **MT7921AU is not sweepable right now.** No `60-wifit3-mt7921au.rules` exists → its usbfs node
  stays `root:root` (kali user can't open it; the 16 Realtek/Ralink/Atheros rules use `GROUP=sudo`).
  Under sudo it opens but **firmware load fails** (`MCU send_bulk failed` / `PATCH_SEM_CONTROL: no
  response`) — classic needs-cold-replug state. When we reach the mt7921 card: replug + confirm a
  wifit3 rule covers it (the app installs rules at runtime; the diag scripts do NOT).
- **Same-chipset AXML + PAU0F share VID:PID `0e8d:7961`** → identical description → `--card` can't
  tell them apart. Sweep them one-at-a-time with distinct `--chip mt7921au_axml` / `_pau0f` slugs
  (or `--card wlanN` if both are plugged, but enum order isn't stable).
- `baseline-linux` untested this session (cards are wifit3-bound, not kernel-bound) — its
  compile/argparse/`--card` wiring is verified, but the airmon path needs a kernel-bound card.

**Suggested plan tweaks to discuss after card #1:**
- WPS row: run `--mode timing … --auto-ack --ack-resend --ack-resends 1 --attempts 5` (campaign-
  faithful ACK behavior + diagnostics). Keep attempts ≤ ~5/round vs the AirLink 30-then-lock budget.
- For the mt7921 pair, decide the udev-rule fix (you own rule install/uninstall) before its sweep.

---

### MT7921AU  (mt7921au) — ALFA AWUS036AXML
*2.4 + 5 GHz 802.11ax · swept 2026-07-24 — no-replug rows only; RX/Port pending baseline flip*

**ACKs** — both axes match ACK-DRIVER-REDESIGN.md; the `❌` in VERIFICATION is **stale** (predates the
FAKE_MAC / active-monitor work).
- auto-ACK (rx_autoack, n=100, ch1): spoofed AM-on **99/100**, AM-off 0, own-silicon 0, bogus 0 →
  SPOOFABLE (auto-ACKs a *spoofed* MAC via active monitor, not its silicon MAC). Matches table (2G 102/0/0/0).
- stop-on-ACK (tx_retries, n=100, ch1): AM-off real AP median **15** (ignores the AP's 1125 ACKs), AM-on
  median **1**; dead target ~14 both ways → needs active monitor. Matches table (2G 13→1, dead ~13).

**WPS** (wps_lab timing, campaign-faithful `--auto-ack --ack-resend --ack-resends 1`, n=5, test router ch1):
assoc **5/5**, reached M5 **5/5**, M7 **5/5**, 0 timeouts. M7 arrival med **2602 ms** (min 2557, max 4086 —
the max is attempt#0 with a 1582 ms cold-bring-up assoc; steady state ~2560–2630 ms). Sniffer (8812)
confirms the card auto-ACKs the AP: **us→AP ACKs ~8/attempt** (AP→us ~6–12). Clean — NOT the
"chatty/fragile, no HW-ACK" the chip doc still describes.

**RX / Baseline A/B**: PENDING — needs the linux-bound flip (baseline-linux) then baseline-wifit3.

**Proposed grade/doc changes (need your approval — I don't edit VERIFICATION/chip-doc unprompted):**
- VERIFICATION `ACKs ❌ → ✅` (or `⚠️` "spoofed MAC only, active-monitor-gated; not silicon MAC"). Grade B may lift.
- WPS is clean (5/5 M7) — supports the existing ✅ and contradicts the chip doc's WPS caveat.
- MT7921AU.md Gotchas "auto-ACK unimplemented" + "WPS chatty/fragile, no HW-ACK" are stale — worth a
  dated debug-log entry / Gotcha correction.
- Bring-up carries the +10 s Linux clear-halt delay (already logged in the chip doc).

---

<!-- PER-CARD TEMPLATE — copy one block per swept card, fill, drop the comment markers.

### <CHIPSET>  (<driver-slug>)
*<adapter> · <bands>* — swept <YYYY-MM-DD>

**RX / Baseline A/B**
- ref2g: wifit3 __/s vs linux __/s (__%).  ref5g: wifit3 __/s vs linux __/s (__%).
- breadth 2.4: __ vs __ · 5 GHz: __ vs __ · RSSI median __ dB · channel-tune __/__ heard.

**driver-health:** __

**ACKs**
- auto-ACK (rx_autoack): spoofed AM-on __/100 (control __) → matches / differs from ACK-DRIVER-REDESIGN (was __).
- ACK-retry (tx_retries): real AP median __ copies (AM off __/on __), dead target ~__ → matches / differs (was __).

**WPS** (wps_lab timing, N=__): assoc __/__ · reached M5 __/__ · M7 __/__ · timeouts __ ·
  M7 latency min/med/max = __/__/__ s · notes: __

**Proposed grades:** RX _ · TX _ · ACKs _ · Port _   (Stress / WEP / PMKID / Handshake deferred to user)
-->
