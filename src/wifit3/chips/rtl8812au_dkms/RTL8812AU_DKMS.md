# RTL8812AU — vendor/DKMS port ground truth

Port of the **RTL8812AU / 2T2R** (ALFA AWUS036ACH, `0bda:8812`) on the shared
`chips/rtl88xxau_base/` jaguar core plus the 8812a-specific modules. **Verification is
byte-for-byte against morrownr's cold-boot captures, plus live RX off the antenna.**

> **STATUS (2026-06-05): RX WORKS, bring-up is byte-for-byte complete.**
> `scripts/rtl8812au_dkms/verify_pcap.py` reproduces morrownr's **entire** cold-boot USB
> conversation **byte-for-byte on BOTH `capture-2` and `capture-3`**: power-on → FW → MAC →
> BB/RF → channel → TX-power → phydm init (incl. the live **EDCCA PSD search**) → monitor
> opmode + set-channel. `rx_diag.py --no-iqk` then hears **real beacons** (27 APs, valid
> frame-control, real OUIs; reproducible across cold boots; ~6.8 MB/12 s). The only
> un-replayed traffic is bulk-IN RX (chip→host, the frames themselves) and post-boot hops.

## How RX was fixed — and the leads that were WRONG (do not re-chase)

The demod produced `crc_err` garbage until the port reproduced morrownr's **monitor /
RX-START tail** (`monitor.set_monitor_mode`): after the phydm EDCCA search, morrownr re-runs
a full channel tune (`set_channel_bw` + `set_tx_power`) before entering monitor opmode. That
re-tune restores the RF/BB into a clean receive state after the EDCCA search has toggled the
LNA / walked the EDCCA thresholds. wifit3's old direct `enter_monitor` skipped it → garbage.
Progression: minimal monitor, no search → 117 KB garbage; + live EDCCA search → 467 KB
garbage; **+ the monitor tail → real beacons.** (Not yet bisected to the minimal sub-write;
the channel re-tune is the prime suspect, RPWM-wake/opmode not ruled out.)

Disproven theories from earlier sessions (kept only in git history — ignore the old notes):

- ~~B-cut~~ → the chip is **C-cut**. `REG_SYS_CFG[15:12] = 1`, and `read_chip_version_8812a`
  adds **+1** for the 8812, so `CUTVersion = 2 = C_CUT`. This corrected two real RF bugs:
  the rCCAonSec CCA toggle ("FIX-2") must **NOT** run (C-cut skips it — `rf._rf_read_cca_off
  = False`), and `phy_FixSpur_8812A` takes the **C-cut branch** (3 writes incl. 0x8C4, run
  per-path inside `phy_SwChnl`), not the 1-write non-C-cut branch.
- ~~"needs runtime IQK"~~ → morrownr receives with **no IQK** (the IQK one-shot is absent
  from all captures). Run `rx_diag.py --no-iqk`. `iqk.py` is a faithful port, kept, unused.
- ~~"the monitor / airmon dance is skippable"~~ → it is **not** (it's the RX fix, above).

## Hardware facts (verified vs the morrownr pcap + EFUSE decode)

- **USB endpoints:** bulk-IN **0x81**, **three** bulk-OUT **0x02/0x03/0x04**, int-IN 0x85.
  → the **3-out-EP** queue map (`TRXDMA_CTRL` low bits **0xF5B0**, and the 3-EP path does NOT
  run `init_hi_queue_config`). TX (M6) must send on bulk-OUT **0x02**.
- **Cut: C-cut** (`CUTVersion = 2`). `REG_SYS_CFG = 0x04411137`.
- **morrownr build flags** (deduced from the bytes): `CONFIG_BEAMFORMER_FW_NDPA` **ON** → TX
  page boundary **0xF7** (not 0xF9) and RXFLTMAP1 NDPA bit (`0x0420`); `CONFIG_RF_POWER_TRIM`
  OFF; **legacy (non-HALMAC)** efuse path.
- **EFUSE:** rfe_type **3**, board_type **0xD8** (ext-LNA/PA both bands), MAC
  `00:c0:ca:ba:be:b5` (ALFA OUI), bb_swing 2.4G 0 dB. Per-nTX PG diffs are **CUMULATIVE**
  (BW20-2S = base + diff[1TX] + diff[2TX]); the TX-power training word compares as **u32**.
- **8051 reset:** REG_RSV_CTRL+1 toggles **BIT3** on the 8812 (BIT0 on the 8821) — threaded
  as `reset_8051_bit` through `base/firmware.bring_up`.
- **Monitor RCR:** morrownr/airmon's tail leaves **0x90000001** (`AAP|APP_PHYST|APPFCS`,
  leaning on RXFLTMAP). That value reproduces airmon's exact chip state but does **NOT**
  deliver management/broadcast frames (beacons) into wifit3's RX pipeline — it lacks the
  accept-mgmt/broadcast class bits and RXFLTMAP alone does not substitute. So the driver
  (M8) re-opens the filter to wifit3's own monitor RCR **0x9000382F** (accept all *good*
  frame classes; CRC/ICV-error frames still dropped) right after the monitor tail —
  **HW-confirmed: clean 2.4 GHz beacons instantly, stable 10+ min on ch1.** The tail's RF
  re-tune is the demod fix; the RCR is a separate *delivery* gate. (The old "RCR is not
  the RX blocker" note was only ever tested via rx_diag's permissive 0x90003B2F override,
  never 0x90000001 alone — which is why the gap surfaced only when the driver ran for real.)
- **Firmware:** `array_mp_8812a_fw_nic`, 27030 B.

## Milestones

| M | Scope | Status |
|---|-------|--------|
| M0–M5 | efuse → FW → MAC → BB/RF → chan → TX-pwr → phydm-init → monitor | **DONE — byte-for-byte on both captures; RX confirmed on hardware** |
| M6 | 2.4 GHz TX (deauth + WEP) | **deauth HW-CONFIRMED: `inject_frame` rides bulk-OUT 0x02 via the shared base fake-txdesc (`rtl8812a_fill_fake_txdesc`). A targeted burst deauthed the client; its reconnect 4-way handshake — incl. M2/M4 (ToDS) — was captured, so a crackable WPA handshake is reachable. No TX pcap exists (the capture's DKMS build injected nothing — 0 bulk-OUT), so the live gate is `scripts/rtl8812au_dkms/deauth_hw.py`, NOT byte-for-byte. The TX engine is enabled by the standard bring-up (no TXPAUSE/REG_CR surgery — proven by frames landing). WEP injection: TODO (WEP test router).** |
| M7 | 5 GHz RX/tune/TX | not started (`chan._switch_band_5g` raises) |
| M8 | `driver.py` + manager wiring | **DONE (code): `Rtl8812auDkmsDriver` satisfies the WlanDriver protocol — claim → bring-up → RX reader (started before the monitor RX gate) → 2-path DIG watchdog → monitor. Gate-faithful (adds only OS-level USB claim + RX, no vendor ops) + unit-tested. Wired in `manager.py` behind `WIFIT3_RTL8812=dkms`; mainline stays the default. **HW-confirmed: clean 2.4 GHz RX via the app (`WIFIT3_RTL8812=dkms uv run wifit3`) — beacons instantly, stable 10+ min on ch1.** Needs the post-tail RCR re-open to 0x9000382F (morrownr's 0x90000001 does not deliver beacons into the pipeline — see Monitor RCR). `inject_frame` is an M6 no-op stub; `set_channel` is 2.4 GHz only (M7).** |
| M9 | A/B vs mainline `chips/rtl8812au/` + flip default behind `$WIFIT3_RTL8812` | not started |

## The byte-for-byte gate

`uv run python scripts/rtl8812au_dkms/verify_pcap.py usb_dumps_new/captures_8812au/<cap>.pcap`
— self-contained, offline, capture-agnostic. Feeds the chip's recorded reads back (so RMWs
and the EDCCA search reproduce) and checks every write byte-for-byte; coverage-audits every
transfer type so a PASS can't hide a blind spot. **Run on both capture-2 AND capture-3** — a
stream that matches one boot but not the other is flattened dynamic behaviour. The **EDCCA
PSD search is reproduced, not stripped**: the replay feeds each boot's recorded PSD, so the
two boots' different-but-deterministic loop lengths both match. The monitor tail is
reproduced via `monitor.set_monitor_mode`.

## Verification coverage — what IS and ISN'T byte-diffed (track every gap)

The gate diffs **every driver-emitted control read/write in the cold-boot bring-up**,
contiguously, **0 skipped**: **6213 ops on capture-2, 6376 on capture-3**. The ~163-op
difference between the two is *only* the **EDCCA PSD search** running longer on one boot's RF
environment — both reproduced byte-for-byte. **There is no untracked byte gap inside the
bring-up.**

Explicitly NOT in the gate (each accounted for, none silent):

- **bulk-IN RX** (~10.8k packets) — chip→host *input* (the received frames), not driver output.
- **The runtime** (~19.9k *control* ops AFTER the first monitor entry):
  - channel **hops** = `set_channel_bw` + `set_tx_power` re-run — **proven byte-identical** to
    the capture's monitor-tail re-tune, so covered by the same code;
  - the **DIG watchdog** = `dig.watchdog_tick` (FA-counter reads + IGI writes) — kernel-
    faithful, but **NOT byte-diffed against the capture's runtime ticks. ← the one ported-but-
    not-byte-verified path; byte-diffing it is the next step for long-run gain stability.**
- **1 µs inter-write delays** — the vendor's BB/RF table walk inserts `ODM_delay_us(1)` after
  each write; the port doesn't. These are **not USB ops** (invisible to a byte diff) and are
  swamped by USB control-transfer latency (~125 µs–1 ms/write ≫ 1 µs). The 8812 RADIO tables
  have **no** `0xfe`/`0xffe` 50 ms-delay rows, so there is no RF-settle-delay gap either.

## Provenance / tooling

- Vendor source + captures: `usb_dumps_new/captures_8812au/` (morrownr 8812au; 8812a HAL in
  `hal/rtl8812a/` + `hal/phydm/`). capture-2/3 are complete; **capture-1 is truncated**.
- Tooling (`scripts/rtl8812au_dkms/`): `verify_pcap.py` (the gate), `rx_diag.py` (live RX
  classifier), `pcap_regtrace.py`, `extract_tables.py` (golden-hashed tables/FW).
- `[SRC]` = vendor C `file:line`. The squishy USB-C→A adapter falls out — confirm
  `0bda:8812` enumerates before each HW run; a vanished device is a fallen plug, not a result.
