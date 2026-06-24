# RTL8822BU

A port of the rtw88-family driver for RTL8822B silicon: 2T2R, 2.4 + 5 GHz 802.11ac, modern iDDMA
firmware path (not the 8051 legacy path). Verified against `usb_dumps/captures_rtw88_8822bu/capture-1.pcap`
and runtime-tested on a TP-Link Archer T3U Plus v1 (`2357:0138`, CUT_D, MP chip). Shares
`chips/rtw88_base/` infra; the init tables and channel-tune are 8822b-specific.

## Status

- Cold init, iDDMA firmware boot, PHY/MAC init, channel tune (2.4 + 5 GHz), MGMT TX: working on hardware.
- Full attack stack confirmed on the Archer T3U Plus: handshake (4-way via deauth), PMKID (active
  extract + passive), WPS (PIN brute + PBC auto-invade), WEP ARP-replay and ChopChop.
- Warm reattach: working (skips bring-up, resumes polling), with a bulk-IN smoke test that surfaces
  "please replug" when the pipe is wedged.
- DIG adaptive RX-gain watchdog (`dynamic.py`): ported but NOT yet HW-confirmed — the A/B (beacon/IV
  rate and `pwdb`-saturation-warning rate, watchdog ON vs OFF) is still owed.
- Not implemented: EFUSE read (accurate TX power, BT coex, real `rfe_option`, crystal_cap), IQK/RF
  calibration, `pwrtrack_init`/`phy_bf_init`/`phy_rfe_init`, `set_antenna`, per-rate TX-power tuning,
  USB 3.0 mode switch (works at USB 2.0 HS).

## Gotchas

**`BIT_HCI_TXDMA_EN` is the load-bearing bit and its position differs from the 8821a legacy path.**
It's BIT(0); guessing BIT(2) makes the chip silently refuse bulk-OUT to the TX path, manifesting as
`USBTimeoutError` during FW upload. A whole cluster of reg.h bit positions differ from the legacy
path (TXDMA_EN, the DDMACH0 chksum bits, the IMEM/DMEM DW/CHKSUM-OK bits, FW_READY=0xC078);
`chips/rtw88_base/registers.py` now matches the kernel exactly.

**Without priority-queue init, MGMT bulk-OUT to EP 0x05 stalls** — the queue has no pages. `init_priority_queue_8822b`
must run before any MGMT TX.

**RX filter is two-stage.** MAC init writes the kernel STA RCR `0xE400220E` (AAP/bit0 clear — drops
client→AP/ToDS), and monitor mode then overwrites it with `0xf410400f` from `_finish_attach`. The
kernel does the same overwrite on the wire. Skip the overwrite and you miss ToDS frames (client→AP),
so handshakes captured from the client side are lost.

**TX-desc differs from the 8821a:** 8822b has `old_datarate_fb_limit = false`, so do NOT set
W4[12:8] = 0x1F the way the 8821a does.

**The ON-section `0x04E0` write is omitted and causes a known pcap byte-diff.** The vendor 8822B
drivers write 1 byte to `0x04E0` after each ON-section register access (addr ≤0xff, 0x1000–0x10ff);
it's present in the in-kernel capture but our `rtw88_base` transport doesn't emit it, so `verify_pcap`
diverges there.

**IGI must adapt or RX breaks both ways.** Frozen at the AGC-table default, RX is either deaf to weak
APs or saturating on a strong one (`pwdb` pinned near 255 → the +143 dBm clamp warning in `rx.py`).
The DIG watchdog exists to fix this; until it's HW-confirmed, treat single-session RX numbers with
suspicion.

**EP layout:** 0x84 IN = RX, 0x05 OUT = HIGH lane (BEACON/MGMT/H2C), 0x06 OUT = NORMAL (BE/BK),
0x08 OUT = LOW (VI/VO). With 3 bulk-OUTs, both BEACON and MGMT qsels map to `out_ep[0] = 0x05`.

## Orientation

RTL8822B: 2T2R dual-band 802.11ac, rtw88 family, modern iDDMA FW path, MP/CUT_D part on the lab card.

Cold bring-up runs chip-ID read (`REG_SYS_CFG1`) → iDDMA FW upload → PHY init → MAC-init-for-RX →
priority-queue init → channel tune. FW upload chunks over EP 0x05 via TX descriptors with iDDMA
register triggers; the blob (`assets/rtw8822b_fw.bin`) is byte-for-byte verified against
linux-firmware's with the 64-byte header stripped. PHY init (`phy.py:phy_set_param`) is a minimal
port of `rtw8822b_phy_set_param` — it loads mac/agc/bb/rf_a/rf_b via the phy_cond walker and uses
`rfe_option=3` (IFEM-ext, the common retail-dongle choice), deliberately skipping crystal_cap,
`config_trx_mode`, `phy_rfe_init`, `pwrtrack_init`, `phy_bf_init`. Channel tune (`chan.py:set_channel_2g_20mhz`
/ 5g) ports `rtw8822b_set_channel`. RX/RSSI decode is in `rx.py`; TX-desc build is `tx.py:build_tx_desc_mgmt`.
Warm-detect is `mac.is_chip_warm`. DIG watchdog is `dynamic.py`, wired in `driver._finish_attach`.

Names match the vendor C (`data_dumps/rtw88-source-v6.18/`), so grep there to cross-reference.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate (currently diverges only at the `0x04E0` ON-section write).
- `extract_init_tables.py` — extracts the mac/agc/bb/rf_a/rf_b init tables from the kernel source.

## Debug log

### 2026-05-25 — cross-driver gap audit

RX polling loop was dropping frames; fixed with a dedicated reader thread + `call_soon_threadsafe`
(port of the rtl8821au fix). HW A/B: beacon rate 7–9/s → 7.9–9.4/s. Same audit found monitor mode
wasn't capturing ToDS frames — the STA RCR drops client→AP — fixed by overwriting RCR with the
monitor `0xf410400f`; HW-confirmed full M1–M4 capture.

### 2026-05-31 — attack stack + warm reattach

Full attack stack passed on the Archer T3U Plus: handshake, PMKID (both paths), WPS (PIN + PBC), WEP
ARP-replay + ChopChop. (WEP fragmentation was removed project-wide — it only ever worked on the
RTL8821AU via a one-card software-sequence TX hack.) Separately, quitting and relaunching wifit3 hit
the warm-reattach path: reattach succeeded, the 1.5 s bulk-IN smoke test found the pipe wedged
(drained 282 stale bytes, then 0 frames in 1500 ms), and the driver logged the replug guidance —
working as designed, same lesson as the 8821a (`pwr_off_seq` cycle doesn't recover bulk-IN on
Windows/WinUSB). Remaining gap is UI-side: the splash shows a generic "Hardware failed to initialize"
instead of the driver's replug message.

### DIG watchdog — adaptive RX gain (ported, not HW-confirmed)

The kernel runs `rtw_phy_dig` every 2 s, walking the OFDM initial gain index (IGI) from the
per-window false-alarm count. `dynamic.py` ports the no-link/coverage path: IGI on `0xc50` (A) /
`0xe50` (B) mask 0x7f (no CCK-IGI, since `dig_cck == NULL`); FA counters at `0xa5c` (cck) / `0xf48`
(ofdm) with the verbatim per-register reset toggle order; IGI bounded [0x1c, 0x2a] with FA thresholds
2000/4000/5000 → step +2/+3/+4 then −2. Seed is kernel-derived: read the current `0xc50` (AGC
default) and converge from there without writing — deliberately unlike the 8814au (which seeds
`DIG_CVRG_MIN` to fight a deaf boot), because the 8822b symptom is the opposite (gain too high →
saturation). Omitted `rtw_phy_dig_check_damping` (linked-mode oscillation guard, a no-op in the
coverage path) and the CRC/CCA stat reads (feed rate-adaptation/CCK-PD, not DIG). The stock driver's
first steady-state write set IGI=0x22. Next: A/B on the Archer T3U — beacon/IV rate and the `pwdb`
saturation-warning rate, watchdog ON vs OFF.
