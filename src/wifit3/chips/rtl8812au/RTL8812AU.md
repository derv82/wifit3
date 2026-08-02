# RTL8812AU (mainline)

Cleanroom port of the in-tree rtw88 88xxA driver, derived from `rtw88-source-v6.18/` and the
cold-boot pcap. RTL8812AU silicon: 2T2R, 2.4 + 5 GHz 802.11ac, legacy MCUFWDL firmware, sibling to
RTL8821AU. **Not the default driver for 0bda:8812** — the vendor/DKMS port (`chips/rtl8812au_dkms/`)
is. Reach this one only via `WIFIT3_RTL8812=mainline`, and only for fixed-channel work.

## Status

Full attack stack verified on hardware (AWUS036ACH, 2026-05-31): deauth, handshake (M1+M2 and
M2+M3), PMKID (passive + active), WPS (PIN + PBC), WEP Replay, WEP ChopChop (<2 min). Cold init +
legacy FW boot, MAC/PHY init, 2.4 + 5 GHz monitor RX, TX inject, EFUSE read, and FW-warm reattach
all working. `verify_pcap` clean against the cold-boot pcap. WEP fragmentation was removed
project-wide (only ever worked on the 8821au via a one-card TX hack).

## Gotchas

**Do not channel-hop dual-band — the RX path wedges and won't recover.** A single channel survives
30 min+, but 0.25 s dual-band hopping kills RX after seconds-to-minutes (non-deterministic): bulk-IN
goes silent while the control plane stays alive — RF18 reads back the right channel and CRC/false-alarm
counters read 0, which points at the RF synthesizer (VCO/PLL) losing lock under thermal drift. This is
an rtw88-inherited hardware limit, not a userland bug — the in-tree driver has it too. The kernel
re-centers the VCO via `rtw8812a_do_lck` but gates it behind `rtw_phy_pwrtrack_need_iqk` (≥8 thermal
drift from the efuse baseline ≈37); hopping holds the chip ~32–42 so the gate never trips. Our
`dynamic.py` decouples `do_lck` from `need_iqk` and runs it on a fixed cadence + each band entry,
which delays the wedge ~2–4× but does not eliminate it. BB reset and full PHY re-init both fail to
revive the pipe — the only recovery is replug.

**Queue regs are write-only load registers.** `REG_RQPN` / `REG_RQPN_NPQ` / `REG_TXDMA_PQ_MAP` always
read back 0 — writes latch the queue config into internal hardware state. The chip needs a fresh
`BIT_LD_RQPN` commit reasonably close to TX time, or the MGMT bulk-OUT NAKs every frame. `_arm_tx_queues`
re-issues the three writes once at `_finish_attach`; that one commit survives to TX time. This is
hardware behavior, not a code bug — nothing to fix at source.

**Card negotiates HighSpeed (USB 2.0), not SuperSpeed despite the box** (`bcdUSB=0x0200`, bulk
`wMaxPacketSize=512`, no SS companion descriptors, PyUSB `dev.speed=3`). rtw88 has `rtw_usb_switch_mode`
to opt into USB 3.0 but never calls it on cold boot, and neither do we — so all the WinUSB SuperSpeed
quirks (RAW_IO bulk-IN serialization, NRDY/ERDY, bulk-OUT ZLP) don't apply here. They would if M-later
ever flips the chip to SS.

**RX needs the monitor RCR filter and RX-DMA aggregation armed, or it dies.** rtw88xxa init leaves
`REG_RCR` byte0 at `0x0E` (AAP cleared) which drops ToDS frames — `apply_monitor_rx_filter` writes the
monitor `0xf410400f`. Separately the FW-default RX-DMA page accumulator wedges bulk-IN after ~5 s of
traffic (clean cliff, control plane alive); `configure_rx_aggregation` ports `rtw_usb_dynamic_rx_agg_v2`
and arms it once at attach.

**2T2R quirks vs the 1T1R 8821au sibling.** RF18 / channel tune / RSSI must cover both paths A and B.
SIPI `read_rf` takes its PI/SI mode-select bit from `REG_3WIRE_SWB` on path B (not SWA) — using SWA for
both corrupted masked RF read-modify-writes on path B. CCK RSSI is rate-branched in `rx.py`
(`parse_jaguar_phy_status_rssi`): CCK uses an lna/vga lookup, OFDM/HT/VHT uses `gain-110` maxed across
active paths. DFS 5 GHz channels (52..144) are excluded from `SUPPORTED_CHANNELS` (no DFS clearance) but
`set_channel` accepts them if asked explicitly.

**Hop-death gives the Scanner no feedback** — targets fade dark, the list empties, no banner. The
driver logs the warning, but the Scanner UI doesn't surface it.

## Orientation

rtw88 88xxA family, 2T2R, wlan CPU = 8051 → legacy MCUFWDL FW path (not the iDDMA path of
8822b/c/8814a). `tx_pkt_desc_sz=40` (like 8821au, not 48), `rx_pkt_desc_sz=24`. Shares
`rtw88xxa_power_on`, the bitfield-rfe `phy_cond` walker, and the 88xxA SIPI/power_seq runtime with
8821au.

Cold path runs from `driver.connect()` → `_cold_bring_up` (power-on → `pre_fw_init` → FW download →
validate → `post_fw_mac_init` → PHY → channel). Warm reattach branches on `mac.probe_chip_state`
(COLD / FW_WARM / FULLY_WARM): FW_WARM skips the FW upload, FULLY_WARM also skips MAC/PHY and just
reattaches + RX-smoke-tests the pipe. Channel/band logic is in `chan.py` + `phy.py`
(`switch_band_*_20mhz`, `set_channel_*_20mhz`). TX desc build is `tx.py`; EFUSE read is `efuse.py`
(feeds rfe_option into phy/RF config — without the real values only the nearest AP was visible).

FW asset is the canonical `linux-firmware/rtw88/rtw8812a_fw.bin` (27030 B = 32 B legacy header +
26998 B body); body byte-verified identical to the pcap-extracted body. Names match the kernel C —
grep `data_dumps/rtw88-source-v6.18/`.

## Scripts

- `extract_rtw8812a_fw.py` — reproduces the FW body from the cold-boot pcap (dev/RE; runtime loads linux-firmware directly).
- `extract_init_tables.py` — parses `rtw8812a_table.c` into the `assets/{mac,agc,bb,rf_a,rf_b}_tbl.py` flat-u32 modules.
- `test_hw_rtl8812au.py` — phase-chain HW test (open→fw→validate→mac_init→phy→channel→beacon, plus efuse/tx phases); auto-skips upload on a warm chip.

## Debug log

### 2026-05-31 — RF-synth hop-death pinned

Dual-band hopping wedged RX non-deterministically (seconds to minutes). Control plane stayed alive
through it — RF18 read back the correct channel and CRC/false-alarm counters were 0 — so the MAC/DMA
were fine and the RF synth (VCO/PLL) was losing lock under thermal drift. The kernel only re-centers
the VCO (`rtw8812a_do_lck`) when thermal drift ≥8 from the efuse baseline trips `need_iqk`, but hopping
keeps the chip ~32–42 so that gate never fires. Decoupling `do_lck` from `need_iqk` (fixed cadence +
band-entry) in `dynamic.py` delayed the wedge ~2–4× but didn't fix it; BB reset and full PHY re-init
both failed to revive the pipe. Concluded it's a hardware envelope limit shared with the in-tree
driver — replug is the only recovery.

### 2026-05-30 — write-only queue regs, EFUSE, per-frame re-arm removed

A Q-state bisect printed all three queue regs (`RQPN`/`RQPN_NPQ`/`TXDMA_PQ_MAP`) as 0 at *every*
checkpoint, including before `post_mac_init_phy` ran — so they're write-only load registers, not
something our code path was clearing. The earlier theory (the 2T2R rf_b table load or an 8812a-specific
phy_bb_config poke clearing the queue state mid-bring-up) was wrong; the readback is always 0 by design.
Moved `_arm_tx_queues` from before-every-inject to a single commit at `_finish_attach` — hw-confirmed on
cold boot, 10/10 deauths out, per-frame re-arm was redundant. Same window: EFUSE read landed
(`efuse.py`), rfe_option resolved to 3 on the AWUS036ACH and fed phy/RF config, which closed the
"only the nearest AP" sensitivity gap.

### 2026-05-17 — M1..M4 bring-up

Cold boot through FW_READY_LEGACY, MAC+PHY init, 2.4 GHz beacons, and TX deauth all landed. M4 deauth
was demo-verified (10/10 bursts, target client reconnected, re-captured the 4-way EAPOL). Path-B SIPI
read bug found here: `rf_sipi.read_rf` was taking the PI/SI mode bit from `REG_3WIRE_SWA` for both
paths; path B reads it from `REG_3WIRE_SWB` (`rtw88xxa.c:1248`), and the wrong reg corrupted masked RF
read-modify-writes on path B (2T2R-only). FW body byte-verified against the pcap the same day.
