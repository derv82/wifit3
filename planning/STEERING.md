# Steering (RX gain / sensitivity tuning) — chipset support

Findings as of 2026-06-25.

"Steering" here means runtime adjustment of the RX front-end gain / sensitivity — the
register the vendor driver's link-tuner / AGC / DIG algorithm walks to trade sensitivity
against noise-rejection. The question this document answers: **does each supported chipset
expose a host-writable gain/sensitivity actuator** (the precondition for wifit3 driving it).

## Summary

**15 of 16 chipsets expose a host-writable gain/sensitivity actuator. Only `mt7921au` does
not** (firmware-owned AGC). The >80% bar is met (~94%).

The actuator and the loop that drives it are **not shared across families** — each family has
its own register, its own control signal, and its own monitor-mode behavior. There are four
distinct mechanisms (Ralink BBP66, Realtek IGI, Atheros ANI, MediaTek AGC_GAIN) plus two
hard-MAC parts with a bare gain register and no loop.

## Per-chipset support

| Chipset | Actuator | Host-writable | Loop in our port | Notes |
|---|---|---|---|---|
| rt2800usb | BBP66 VGC | yes | channel-config only | link_tuner ported; no monitor loop |
| rt3070 | BBP66 VGC | yes | channel-config only | link_tuner ported; no monitor loop |
| rt5370 | BBP66 VGC | yes | **yes (monitor)** | monitor link-tuner added this cycle |
| rt5372 | BBP66 VGC | yes | channel-config only | link_tuner ported; no monitor loop |
| rt2500usb | BBP R17 | yes | none (init seed) | hard-MAC; no link_tuner callback exists |
| ar9271 | ANI immunity state machine | yes | **none** | no ANI code in our port; runs at init defaults |
| rtl8188eus | IGI @ 0xc50 | yes | DIG watchdog | mainline + dkms variants |
| rtl8812au | IGI @ 0xc50 | yes | DIG watchdog | mainline + dkms variants |
| rtl8821au | IGI @ 0xc50 | yes | DIG watchdog | mainline + dkms variants |
| rtl8821cu | IGI @ 0xc50 | yes | DIG watchdog (dkms) | |
| rtl8814au | IGI @ 0xc50 | yes | DIG watchdog | dkms |
| rtl8822bu | IGI @ 0xc50 | yes | DIG watchdog | mainline (rtw88) + dkms |
| rtl8187 | rtl8225 RF gain | yes | none | hard-MAC; RF-gain path, no baseband DIG/VGC |
| mt76x0u | AGC_GAIN (bits 14:8) | yes | none (init adjust) | mt76x02 family |
| mt76x2u | AGC_GAIN (bits 14:8) | yes | none (init adjust) | mt76x02 family |
| mt7921au | — firmware-owned — | **no** | — | connac2; PHY/AGC in firmware |

mainline-vs-dkms variants of the same Realtek silicon are independent codebases but share the
same actuator (IGI @ 0xc50).

## Actuator mechanisms by family

**Ralink rt2x00 (rt2800usb, rt3070, rt5370, rt5372) — BBP66 VGC.**
Single baseband register. `rt2800_link_tuner` adds `+0x10` to the default VGC when RSSI beats
−80 dBm (the default arm; RT3572/3593/3883/5592 use other steps/thresholds) [SRC
rt2800lib.c:5787-5830]. Default VGC for this silicon is `0x1c + 2*lna_gain` [SRC
rt2800lib.c:5728-5741]. Write is guarded — only emitted when the level changes [SRC
rt2800lib.c:5759-5780]. The kernel runs this **only for STA/AP vifs** (`intf_sta_count`); a
monitor-only interface never schedules it [SRC rt2x00link.c:191, 228, 358], so BBP66 stays at
the per-channel-config default. rt5370 now runs the heuristic in monitor on a ~1 Hz task
(`rt5370/link_tuner.py` + `driver.py::_agc_loop`); the other three port `link_tuner.py` but
write BBP66 only at channel config (`reset_tuner`).

**Ralink rt2500usb — BBP R17.** Hard-MAC part. R17 is the variable gain, seeded from per-card
EEPROM BBP-tune words at init; "the only gain-control mechanism on this part — rt2500usb has no
periodic link_tuner callback" [SRC rt2500usb/bbp.py:143-150]. Writable; no loop in vendor or
our port.

**Atheros ar9271 (ath9k_htc) — ANI.** Not a single register: a multi-level noise-immunity state
machine — OFDM and CCK immunity levels, spur-immunity level, firstep level, OFDM weak-signal
detection, MRC-CCK — applied via `ath9k_hw_ani_control` [SRC ani.c:149-271]. Driven by PHY-error
counts. Our ar9271 port implements **none** of it; the driver sets only the monitor RX filter
[SRC ar9271/driver.py:385-409] and otherwise runs at hardware/init defaults.

**Realtek (rtw88 mainline + phydm/ODM dkms) — IGI @ 0xc50.** Single OFDM initial-gain index,
`ODM_REG_IGI_A_11AC`, field `[6:0]`, written on change. `rtw_phy_dig` walks IGI from the
per-window false-alarm count: high FA raises IGI (back off gain), low FA lowers it [SRC
rtw88/phy.c:536-590]. Unlinked (no station) it uses coverage bounds `[DIG_CVRG_MIN=0x1c,
DIG_CVRG_MAX=0x2a]`, FA thresholds 2000/4000/5000, steps `{+4,+3,+2}` then `−2` [SRC
rtw88/phy.c:456-507]. `RTW_FLAG_DIG_DISABLE` disables it; `rtw_phy_dig_set_max_coverage` pins
minimum IGI. Our ports carry this as a live DIG watchdog — confirmed reading
`rtl8821cu_dkms/watchdog.py::_dig` (reads FA, writes 0xc50 on change, clamp `[0x1c,0x2a]`) and
`rtl8822bu/dynamic.py` (same algorithm).

**MediaTek mt76x02 (mt76x0u, mt76x2u) — AGC_GAIN.** AGC gain field (bits 14:8), read-add-write
via `mt76x2_adjust_agc_gain` / `mt76x02_phy_adjust_vga_gain`, driven by the false-CCA count:
`false_cca > 800` raises the gain adjust, `< 10` lowers it [SRC mt76x02_phy.c:169-191]. Our
mt76x2u port ships the gain-adjust functions (`mt76x2u/phy.py::_adjust_agc_gain`,
`apply_gain_adj`) used at init; no periodic loop.

**MediaTek mt7921au — none.** connac2; "the PHY/AGC live in firmware, so there is no host-side
DIG/IGI/AGC loop to port" [SRC mt7921au/MT7921AU.md:4-5]. No host actuator.

## Control-signal variance ("gain/tuning firmwares" differ)

No two families steer off the same signal, and none steer off a chosen BSSID's RSSI:

- **Ralink:** RSSI — EWMA of associated-BSS beacons (hardware `MY_BSS` filter), threshold/step
  [SRC rt2x00link.c:205-212]. The EWMA is `DECLARE_EWMA(rssi, 10, 8)` — 1/8 weight, fixed-point
  [SRC rt2x00.h:258].
- **Realtek:** false-alarm count (false-CCA / OFDM-FA) → IGI.
- **Atheros:** PHY-error counts → immunity-level transitions.
- **MediaTek mt76x02:** false-CCA count → AGC gain offset.
- **MediaTek mt7921:** firmware-internal, not exposed.

## Monitor / unassociated behavior

The mode in which wifit3 operates is exactly the mode the vendor loops least optimize:

- **Ralink:** link-tuner gated off entirely (`intf_sta_count == 0`); BBP66 holds the
  per-channel-config default [SRC rt2x00link.c:191, 228, 358].
- **Atheros:** `ath9k_ani_reset` pins OFDM/CCK immunity to `*_DEF_LEVEL` (default, not minimum)
  when `opmode != STATION && != ADHOC` [SRC ani.c:327-349]. The STA-mode weak-signal-detection
  boost (`weak_sig = true` when `BEACON_RSSI <= THR_HIGH`) is not applied in monitor for
  pre-AR9300 silicon (AR9271 is AR9002-class) [SRC ani.c:184-200].
- **Realtek:** DIG still runs unlinked, on the coverage bounds `[0x1c, 0x2a]` [SRC
  rtw88/phy.c:463-473].
- **MediaTek mt76x02:** false-CCA driven; runs regardless of association.

## Not host-controllable

- **mt7921au** — firmware-owned PHY/AGC; no host actuator [SRC mt7921au/MT7921AU.md:4-5].
