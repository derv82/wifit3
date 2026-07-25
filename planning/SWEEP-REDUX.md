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
