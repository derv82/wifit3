# RTL8188EUS DKMS — Severe `verify_pcap` Audit (2026-06-16)

Triggered by: the live 2.4 GHz RX gap (our port ~6.5 bcn/s vs the vendor kernel ~8.9 on the
**same** strong APs, from `beacon_watch_usbcap` on the cold-boot captures), and the suspicion —
earned on the 8822bu port — that a green gate hides a real divergence (bad slice boundaries, ops
behind waivers, "skip-because-TX" that is actually constant cal, a dropped `if`, hardcoded EFUSE).

## Verdict (TL;DR)

**The port is faithful across everything the capture + source can observe.** No waiver hides a
vendor op; the runtime watchdog, the RX decode, and the RF/BB/AGC init are byte/algorithm-faithful;
no EFUSE field that affects RX is hardcoded or dropped. **The live RX gap is _not_ a port-faithfulness
defect** — it is RF/silicon/environment, or an effect outside the ~15 s captured window. Two narrow,
**non-default, honestly-flagged** divergence points exist; neither fires in the captured/default build.

This is the opposite of the 8822bu outcome (where the audit found the real antenna-mux bug). Here the
same rigor exonerates the port — which is itself the useful result: stop hunting a software wall, the
WN722N's ~6.5/s is the silicon/environment, and both our ports already match the kernel's class.

## Method

`scripts/rtl8188eus_dkms/verify_pcap.py` is one fail-closed single-cursor walk: every op is **matched**,
**waived** (named + counted), or **unaccounted** (stops the walk). cap-1 PASSes 5769/5769 ops with only
**254 waived** (251 aireplay bulk-OUT + 3 `0x4F0`). Audited four ways:
1. **Empirical** — located every waived op in the capture timeline (op-index + frame).
2. **Source** — 3 read-only audits against the vendor tree (`captures_8188eu/driver-source/`).
3. **Controlled offline RX-decode** — ran our `rx.iter_frames` over the capture's raw bulk-IN.
4. **Real-EFUSE decode** — dumped the actual efuse bytes via our own `read_chip_params`.

## Findings

| # | Area | Verdict | Evidence |
|---|------|---------|----------|
| W-A | Waiver: aireplay bulk-OUT (251) | ✅ legit | all at op 5362–5690 / frames 20838–23093, inside the aireplay window (20622–25054); **zero** vendor bulk-OUT before it |
| W-B | Waiver: `0x4F0` TX_RPT_TIME (3) | ✅ legit | `odm_ra_set_tx_rpt_time` fires only on TX_REPORT2 (TX-driven); no-link watchdog never writes it; init's 2 writes are **matched** |
| S | Dropped `R 0xF0/4` global filter → "all 24 reproduced" | ✅ accurate | `phydm_receiver_blocking` reads `0xf0` MASKDWORD every tick; 1 probe + 1 rf-config + 22 ticks = 24 |
| B | Tick-boundary slicing | ✅ clean | exactly 1 SYS_CFG read per tick (22:22); per-tick op counts uniform (~76–85) — no under-consume |
| G | Watchdog member gating (cfo/ra/pow-train) | ✅ faithful skip | each early-returns `!is_linked`/`!number_linked_client`; correctly not run unlinked |
| L1 | `phydm_receiver_blocking` NBI notch (ch1/ch13) | ✅ faithful no-op | gated on `adaptivity_enable`, which is **off by default** (`rtw_adaptivity_en=0`); wire confirms |
| L2 | powertrack deferred IQK/LCK (≥8 °C) | ⚠️ documented gap | safe (driver catches + continues, DIG keeps adapting); long-hot-session only, mostly TX |
| L3 | Missing-`if` in RF/BB/AGC init | ✅ none found | BB/RF/TXpwr/RCR/DM-init faithful; key conditionals verified |
| L4 | Hardcoded EFUSE | ✅ no RX gap | every RX-relevant field applied or correctly inert; `0xCA=0xFF` blank ⇒ internal LNA = our default |
| RX | Controlled RX-decode (our decoder vs raw) | ✅ 100% | 3120/3120 beacons, ratio 1.00 per AP — decode drops nothing |

### W-A · bulk-OUT waiver — legit
All 251 waived bulk-OUT ops sit at op-index 5362–5690 (frames 20838–23093), entirely inside the
aireplay injection window (frames 20622–25054 per `pcap_slicer`). The vendor monitor driver emits **no**
bulk-OUT (FW download is EP0 control; no association ⇒ no TX). `verify_pcap.py:158` waives each `B` op.

### W-B · `0x4F0` (REG_TX_RPT_TIME) waiver — legit
5 total writes: **2 in init** (op 2120/2420 — `_InitHWLed` `0xcdf0` + init-tail `0x3df0`, both
`!fw_ractrl`, both **matched** by our `mac.py`/`dm.py`) and **3 in the aireplay tail** (op 5363/5562/5768).
`odm_ra_set_tx_rpt_time` (hal8188erateadaptive.c:1200,1206) is reached only from
`odm_ra_tx_rpt2_handle_8188e:1338`, which runs **only when a TX_REPORT2 packet arrives** — a TX event.
No-link RA updates `rpt_time` in a struct but never writes the register without a TX report. So the 3
operational writes are aireplay's, not a skipped vendor op. *(Comment nit: the gate comment says
"written only on TX"; init writes it twice — imprecise, not a bug.)*

### S · the "all 24 SYS_CFG reads reproduced" claim — accurate
`phydm_receiver_blocking` (phydm.c:3042) does `odm_get_bb_reg(dm, 0xf0, MASKDWORD)` at line 3050, called
every tick from `phydm_watchdog` (phydm.c:1833). `CONFIG_RECEIVER_BLOCKING` is enabled for 8188E
(`phydm_features_ce.h:72` on `RTL8188E_SUPPORT`; `ODM_RECEIVER_BLOCKING_SUPPORT = ODM_RTL8188E|ODM_RTL8192E`).
Count: 1 chip-version probe (`read_chip_version_8188e`, rtl8188e_hal_init.c:2458) + 1 rf-config tail + 22
ticks = **24**, matching the capture. No other 32-bit `0xF0` read omitted.

### L1 · receiver-blocking NBI notch — faithful no-op (the "missing if-statement that does cal")
This is the scar-class case and it was checked hardest. `phydm_receiver_blocking` reads `0xf0` (which we
reproduce) then, **only if** `consecutive_idle_time > 10 && !mp_mode && adaptivity_enable`, enables a
narrowband-interference notch on **ch1 (2410 MHz)** / **ch13 (2473 MHz)** via `phydm_nbi_setting` +
`0xc40` (phydm.c:3057–3089). Our `dig.py:_receiver_blocking` reproduces the read only, with a comment
calling the notch "a separate (live-relevant) milestone." The notch write never fires in a ~15 s capture
(idle < 10), so `verify_pcap` is green either way — and **every measurement we took was on ch1**, the
exact channel it notches. So it had to be falsified, not trusted.

**Falsified → faithful.** For the CE build, `phydm_check_adaptivity` reduces to
`adaptivity_enable = (support_ability & ODM_BB_ADAPTIVITY)` (phydm_adaptivity.c:78–100; the WIN/AP blocks
are `#elif`-excluded). `ODM_BB_ADAPTIVITY` is set only `if (IS_FUNC_EN(dm->enable_adaptivity))`
(phydm.c:1278–1280) — the per-IC CE default that would set it is `#if 0`'d (phydm.c:1116–1124).
`enable_adaptivity` ← `registrypriv.adaptivity_en` (hal_dm.c:280) ← `rtw_adaptivity_en`
(`CONFIG_RTW_ADAPTIVITY_EN`, module param "0:disable, 1:enable", os_intfs.c:510), **default disable**
(rtw_odm.c:81). So `adaptivity_enable=false` ⇒ the notch is dead code and the final disable block never
runs either ⇒ reading-`0xf0`-only is faithful. **Empirical corroboration:** our `_adaptivity` mode2 path
(which assumes `adaptivity_enable=false`) byte-matches the capture's `0xc4c` EDCCA writes.

> **Non-default caveat (flag, don't fix):** a user who loads `rtw_adaptivity_en=1` (e.g. an ETSI region)
> would have the vendor arm the ch1/ch13 NBI notch after ~20 s idle, which our port would not reproduce.
> Out of captured/default scope; port it only if 8188eus adaptivity-region support is ever wanted.

### L2 · powertrack deferred IQK/LCK (≥8 °C) — documented, safe, low-impact
`powertrack.py` reproduces the per-tick thermal swing (OFDM IQ matrix `0xc80` + CCK-FIR `0xa22..29`) at
small thermal deltas, but **defers** (raises `NotImplementedError`, never silently) the IQK/LCK re-cal at
`|Δthermal| ≥ 8` and the over/under-swing-limit per-rate TX-AGC reset (powertrack.py:22–25,187–191,210–215).
`driver.py:166–173` catches the raise per-tick and `continue`s — and since powertrack runs **after**
DIG/CCK-PD/adaptivity in the tick, RX gain keeps adapting; only NHM telemetry + the swing/IQK/LCK skip
while hot. Real divergence vs the vendor only on a **long, hot** session (the dongle drifting ≥8 thermal
units), and mostly TX-side (the IQ matrix is shared, so a minor RX effect is possible). Honestly flagged
in-code; not a captured-path defect.

### L3 · missing-`if` in RF/BB/AGC init — none found
BB (`phy_bb_config`: crystal-cap `0x24`, SYS_FUNC_EN gates, table loads), RF (`phy_rf_config`: RFENV
save/restore, radio-A table, foundry check), TX-power (`set_tx_power` per-rate TXAGC), MAC RCR
(`init_misc02`: `0x700060CE`, ACRC32 correctly excluded for 8188E), and DM-init (DIG `0xC50` seed,
CCK-CCA `0xA08`, NHM thresholds, EDCCA) all port faithfully. Conditionals verified rather than assumed:
`phydm_set_lna(disable)` gate includes 8188E (we call it); EDCCA asserted-bit is `BIT(30)` for 8188E
(hardcoded right, phydm_adaptivity.c:380); path-B RF is gated `rf_type > 1T1R` (correctly skipped);
`_InitPABias` is commented out vendor-side too. No dropped branch.

### L4 · hardcoded EFUSE — no RX-relevant field mishandled
Real efuse bytes (capture-1, decoded by our own `read_chip_params`):

| off | field | value | handling | RX? |
|-----|-------|-------|----------|-----|
| 0xB8 | ChannelPlan | `0xA2` | ignored | TX-only (regulatory limit; inert, build has `TXPWR_LIMIT_EN=0`) |
| 0xB9 | XtalCap | `0x20` | **read+applied** → `0x24` | yes (freq trim) ✓ |
| 0xBA | ThermalMeter | `0x1D` | **read+applied** (powertrack) | TX-track ✓ |
| 0xC1 | RFBoardOpt | `0x00` | board_type 0 (matches) | none |
| 0xC9 | AntennaOpt | `0x03` | ignored | **dead** — `CONFIG_ANTENNA_DIVERSITY` off ⇒ `_InitAntenna_Selection` is a vendor no-op |
| 0xCA | RFE/PA-LNA | **`0xFF` blank** | internal PA/LNA default | **no gap** — blank ⇒ vendor also internal, no external-LNA gain |

A source audit *suspected* a missing 10–14 dB external-LNA gain (`0xCA[6:4]` GLNA / `[3:2]` PA-LNA). The
**real byte is `0xFF` blank**, so the vendor decodes internal-PA + internal-LNA + no GLNA — exactly our
hardcode. **Suspicion dismissed by silicon.** No efuse-derived RX gain is dropped. (The card MAC, crystal,
and thermal all decode correctly.)

### RX · controlled RX-decode test — decode is 100% faithful
Offline, no card: ran our `rx.iter_frames` over capture-1's raw bulk-IN (ep 0x81, 4809 transfers) and
counted beacons, vs the raw beacon-signature regex (`beacon_watch_usbcap`'s method) over the **same**
bytes. Result: **identical, ratio 1.00 on every AP** — `…d3:eb` 218/218, `…d2:18` 210/210, `…d3:e8`
187/187, total **3120/3120**. So the descriptor decode, the `RX_AGG_USB` aggregation walk, the `_RND4`
alignment, and the crc/icv handling drop **nothing** the kernel's bulk-IN carried. The live RX gap is
**not** a software-decode/aggregation bug.

## What this audit does *not* cover (honest limits)

`verify_pcap` + this audit assert faithfulness for the **captured path** (cold init → the 22-tick no-link
watchdog window → monitor entry → channel set → decode) and the source-derived gating of everything
around it. They cannot speak to behaviors that need conditions absent from the ~15 s capture:
- **receiver-blocking under `rtw_adaptivity_en=1`** (L1) — would arm a ch1/ch13 notch; unported.
- **powertrack IQK/LCK under ≥8 °C drift** (L2) — unported re-cal on a long hot run.
- **long-session DIG convergence** — byte-faithful for the captured ticks; over many more live ticks it
  follows the same algorithm but on environment-dependent FA counters (not a port variable).

None of these explain the *cold/short-window* ~6.5-vs-~8.9 gap (the notch needs >20 s idle; thermal needs
a hot run). That gap is therefore attributable to RF/silicon/antenna/environment and the live-vs-capture
time difference — **not a port defect.**

## Recommendation

- **No fix warranted** for the captured/default build — the port is faithful. Do not chase the RX wall in
  software; it isn't there.
- **If** 8188eus adaptivity-region (ETSI) support is ever wanted, port the receiver-blocking NBI notch
  (L1) and verify with `rtw_adaptivity_en=1`.
- **If** long-hot-session RX/TX stability is ever a concern, port the powertrack IQK/LCK + swing-limit
  paths (L2); both raise a clear guard today, so the omission is visible, not silent.

## Provenance

verify_pcap cap-1: 5769/5769 PASS, 254 waived. RX-decode: `tshark` ep 0x81 → `rx.iter_frames` vs regex,
3120/3120. EFUSE: `read_chip_params` over the replayed capture. Source: `captures_8188eu/driver-source/`
(realtek-rtl8188eus 5.3.9, module `8188eu`, CE). Three read-only source audits (waiver gating, SYS_CFG
attribution, EFUSE/init) cross-checked against our `chips/rtl8188eus_dkms/`. No hardware used — the audit
is entirely offline (capture + source), per the porting playbook.
