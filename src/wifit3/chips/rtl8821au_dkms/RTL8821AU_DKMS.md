# RTL8821AU — vendor/DKMS port ground truth

Cleanroom re-port of the **RTL8821AU / RTL8811AU** (ALFA AWUS036ACS, `0bda:0811`)
from the **Lucid-Duck `8821au-20210708` 5.12.5.2** vendor source — the
DKMS-distributed out-of-tree `rtl88xxau` driver (Realtek PHYDM/ODM stack), **not**
mainline `rtw88`. The two are different codebases; addresses/macros/flow come from
the vendor tree (`usb_dumps_new/captures_rtl8821au/driver-source/`) cross-checked
against the cold-boot pcap, never from the mainline-derived `chips/rtl8821au/`.

- **Sibling, not a replacement.** `chips/rtl8821au/` (mainline) stays. Both
  register for `0bda:0811`, ordered by **`$WIFIT3_RTL8821`** (blank/`dkms` →
  this port, the default once HW-proven; `mainline` → fallback). Read fresh per
  run, so it flips between runs without a restart.
- **Why this port:** the mainline-derived port inherits `rtw88`'s weaker 2.4 GHz
  monitor RX (AGC/DIG). The vendor PHYDM DIG path is the suspected fix (proven
  3.6× on 8822bu; 8821au's own breadth is a tie/stability play — the headline
  payoff is the 8812au sibling, deferred until after this port's A/B).
- **Scope:** 20 MHz primary only (no 40/80). SW-seq fragmentation is **not**
  reintroduced (hwseq only). `# TODO(8812au):` breadcrumbs mark every point the
  vendor source branches on chip (RF path 1×1 vs 2×2, RFE option, pwr-seq table,
  FW blob, per-rate txpower) so the 8812au decision is cheap later.

## Potential known gaps (audit as the port lands)

- [ ] RX poll on the event loop vs a dedicated reader thread (start reader
  **before** RX-enable — kernel posts URBs at probe).
- [ ] FCS strip before the RX callback (family invariant: `frame_end == MPDU_end`).
- [ ] Always-monitor RCR / RX_FILTR_CFG / address-match rewrites (vendor inits for
  STA; wifit3 is always-monitor).
- [ ] DIG/IGI watchdog tick (gain freezes at the AGC default without it).
- [ ] 5 GHz: RX + tune + TX must all be ported (do not declare "done" on 2 GHz).

## A/B methodology (vs mainline `chips/rtl8821au/`)

Flip with `$env:WIFIT3_RTL8821='mainline'` vs blank/`dkms`. Fixed channel, equal
dwell, replug between runs.

**Metrics tracked every run:**
- total distinct APs (**nAPs**)
- **total** beacons/s and **peak-AP** beacons/s
- **Canary AP — `NETGEAR2G`, BSSID `aa:bb:cc:dd:ee:01`:** its **RSSI (dBm)** and
  **beacons/s**. NETGEAR2G is a strong, nearby router (strongest in range; ~9–10
  beacons/s when healthy) and is the **DIG-health indicator** — a strong AP whose
  beacon rate sags first when the initial gain (DIG/IGI) is mistuned.

> PII: this BSSID is recorded deliberately as the fixed A/B canary. It is on the
> planned git-history PII-scrub list — do not treat it as an exception to the
> "no real BSSIDs in commits" rule for any *other* network.

**Decision rule (inherited from the 8814au A/B):** breadth and canary rate can
trade off. The 8814au DKMS port won on a run showing roughly **−10 canary beacons
but +10 nAPs** vs mainline — broader reach plus the DIG fix that recovered the
canary's *relative* rate settled it. Don't fail this port on a small canary-beacon
delta if nAPs rises and the canary's RSSI/rate is no longer anomalously low vs its
neighbours.

**Mainline baseline — `chips/rtl8821au/`, 2026-06-04, ch1, 30 s:** 22 APs, 2727
beacons; canary `NETGEAR2G` ≈ 230 beacons (**~7.7/s**) — already *below* its ~9–10/s
healthy rate, consistent with the mainline DIG softness this port targets.

## Milestone status

| M | Scope | Offline (replay-diff) | Live HW | Status |
|---|---|---|---|---|
| M0 | Branch + baseline + `scripts/rtw88_pcap_replay.py` + scaffold | — | — | **done** |
| M1 | Power-on → FW download → FW-ready (+ warm reset) | **PASS** (1627 ops byte-exact, incl. 30848 B FW page-write) | **PASS** cold (SYS_CFG=0x04412135) + warm re-entry, WINTINI_RDY | **done** |
| M2 | MAC init (REG_CR → MACTXEN\|MACRXEN) | **PASS** (182 ops byte-exact, 98-entry MAC table) | **PASS** (REG_CR=0xFF) | **done** |
| M3 | BB/PHY + RF init (PHY_REG/AGC_TAB/RadioA, 1×1) | **PASS** (586 ops byte-exact, JaguarSeries phy_cond walker) | **PASS** (xtal=0x9e7) | **done** |
| M4 | 2 GHz channel tune, 20 MHz (RF-SIPI) | **PASS** (74 ops byte-exact, incl. 8811au ant-prologue) | **PASS** (`--phase chan`, RF[0x18] ch1@20M) | **done** (TX-power deferred) |
| M5 | 2 GHz RX + PHYDM RSSI/DIG (value milestone) | replay-diff | `--phase beacon`, A/B canary | — |
| M6 | 2 GHz TX (deauth + WEP replay) | replay-diff | **user** (TX) | — |
| M7 | 5 GHz: RX + tune + TX | `verify_channels` (5 GHz 36..165) | `--phase beacon` 5G + **user** deauth | — |
| M8 | Driver Protocol wiring + warm reattach + manager `WIFIT3_RTL8821` | — | `--phase open` warm + beacon | — |
| M9 | A/B matrix + flip default to DKMS | — | RX (Claude) + TX (user) | — |

## Live HW access

Milestones are developed + verified **offline** against the cold-boot pcap via the
replay-diff gate (no device needed). Live smoke tests (`test_hw.py`) are a
secondary confirmation and require the ACS to be free: if a `wifit3` app instance
(or another session) holds it, `claim_interface` returns "Access denied" *before*
any bring-up runs — that is the holder, not a wedge and not a port defect. A true
USB wedge (handle open denied even with no holder) needs a one-time replug, which
only the user can do. Warm re-runs from a prior bring-up are fine: `card_enable_flow`
begins with CARDDIS→CARDEMU and re-inits a partially-powered chip.

## Provenance

- Vendor source: `usb_dumps_new/captures_rtl8821au/driver-source/` (Lucid-Duck
  `8821au-20210708` 5.12.5.2, branch `kernel-6.18-compat`).
- Cold-boot captures: `usb_dumps_new/captures_rtl8821au/capture-{1,2,3}.pcap`
  (+ `_logs/main.log` for `pcap_slicer.py`, `iw.log` for per-channel windows).
- Acceptance gate: `scripts/rtl8821au_dkms/verify_pcap.py` +
  `verify_channels.py` over `scripts/rtw88_pcap_replay.py`.
- `[SRC]` = vendor C `file:line`; `[WIRE]` = cold-boot pcap frame.
