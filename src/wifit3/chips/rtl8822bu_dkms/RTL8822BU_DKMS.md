# RTL8822BU — vendor/DKMS port (playbook)

> **STATUS: M0 (chip-ID reads) COMPLETE.** Transport (+0x4E0 mirror), constants, the
> HALMAC chip-id/cut + USB intf-phy + chip-version reads all reproduce **byte-for-byte,
> 13/13 ops, on capture-1/2/3** (gate frontier now at the EFUSE read). Next milestone =
> the early EFUSE map read. Promote sections from plan to ground truth with
> `[SRC]`/`[WIRE]` citations as facts get confirmed; by the end this should read like
> `chips/rtl8812au_dkms/RTL8812AU_DKMS.md`.

## Verified facts (ground truth so far)

- **USB identity / endpoints** `[WIRE]` capture-1/2/3 + usb-topology.log: `2357:0138`
  (TP-Link Archer T3U Plus), single config. Card traffic: control ep0; **bulk-OUT 0x05**
  (FW/TX); **bulk-IN 0x84** (RX). The coverage audit shows no other channel.
- **Register IO is the standard Realtek `bRequest=0x05` vendor control transfer**
  (`READ=0xC0/WRITE=0x40`, addr in wValue) `[SRC] include/usb_ops.h:19-22` — byte-identical
  to the AU family, so the USB layer is *not* HAL-specific.
- **8822b register-page-switch mirror** `[SRC] os_dep/linux/usb_ops_linux.c:171-201`:
  every vendor access to an **ON-section** register (`addr <= 0xFF` or `0x1000..0x10FF`) is
  followed by an extra 1-byte `bRequest=0x05` write to **`0x4E0`** carrying the low byte of
  the IO buffer (read-back value for a read, written value for a write). OFF/LOCAL-section
  regs (incl. `0xFExx/0xFFxx`) get no mirror. Reproduced in `transport.py`; `[WIRE]` every
  `R 0x00fc=0x0a → W 0x4e0=0x0a` pair in the capture confirms it.
- **Re-runnability = clean reset every time, NOT skip-style warm-reattach.** The vendor's
  own power-on handles a still-powered chip: `mac_pwr_switch_usb_8822b` reads `REG_CR`(0xEA
  marker), `REG_MCUFW_CTRL`(0xC078=FW-exist), and `REG_SYS_STATUS1+1` BIT0 to detect
  power state, and returns `HALMAC_RET_PWR_UNCHANGE` if already on
  `[SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:32-98`. `rtw_halmac_poweron`
  catches that and **forces power-OFF (`card_dis_flow`) then power-ON again** — "Work around
  for warm reboot but device not power off" `[SRC] hal/hal_halmac.c:2744-2774`. So our bring-up
  prepends this reset; no replug needed, no warm-skip path. The power-OFF table is not in any
  cold capture (cold boots return SUCCESS, so the off→on never fired), so it is ported from
  source and proven by a hardware double-run. *Open: whether the off→on cycle recovers on
  Windows+WinUSB (the AU/jaguar cycle did not; 8822b `card_dis_flow` is a different sequence).*
### M0 — chip-ID reads (CLEARED, byte-for-byte on capture-1/2/3)

The pre-power-on probe, ported from source and gate-clean (ops 0–12; de-mirrored):
1. **`get_chip_info`** `[SRC] halmac_api.c:517-521` — HALMAC chip-id/cut detection, the very
   first IO: `R 0xFC` (`REG_SYS_CFG2` → `chip_id`; `0x0A` = 8822B) and `R 0xF1`
   (`REG_SYS_CFG1+1 >> 4` → cut; `0x3` = D-cut). → `chipid.get_chip_info`.
2. **`phy_cfg_usb_8822b`** `[SRC] halmac_usb_8822b.c:107` → `parse_intf_phy_88xx`
   `[SRC] halmac_common_88xx.c:3168` over the USB2 (empty) then USB3 param tables
   `[SRC] halmac_phy_8822b.c:40-58`. The D-cut USB3 entry `{0x0001, 0xA841}` is emitted by
   `usbphy_write_88xx` `[SRC] halmac_usb_88xx.c:475` as `W 0xFF0D=0x41` / `W 0xFF0E=0xa8` /
   `W 0xFF0C=0x81` (data-lo / data-hi / `offset | BIT(7)` strobe). `usb_page_switch` is a
   no-op for USB3. → `usbphy.phy_cfg_usb`. *(This was the op0 mystery — not a debug/scratch
   write but the USB3 intf-phy param for this cut.)*
3. **`read_chip_version`** `[SRC] rtl8822b_ops.c:173` — `R32 0xF0/0xF4/0x68`. → `chipid.read_chip_version`.

### Next frontier — the early EFUSE map read (op #13, `R 0x0A` ...)

On the wire the chip-info path reads EFUSE **up front** (before power-on), not at M4 as the
milestone table guesses: `read_adapter_info` = `rtl8822b_read_efuse` → `EFUSE_ShadowMapUpdate`
`[SRC] rtl8822b_ops.c:637,3930` — the `0x0A/0x35/0x37` efuse power/clock setup then the
`REG_EFUSE_CTRL` (`0x30`) physical-map loop (`W 0x316000NN` / `R 0xB16000xx`). This is the
next thing to port; it likely pulls the M4 EFUSE work earlier than the table implies.

### Coverage gap — USB2-link branches untested (all captures are USB3)

capture-1/2/3 are all **SuperSpeed (USB3)** links, so every link-speed conditional is only
verified on its USB3 side. The discriminator is `REG_SYS_CFG2+3 == 0x20` (== USB3, else USB2)
`[SRC] halmac_usb_88xx.c:48`. A USB2-link capture (plug the card behind a USB2-only
adapter/hub so it negotiates High-Speed) would let `verify_pcap` confirm the USB2 sides of:
- `pre_init_system_cfg`'s `0xFE5B |= BIT(4)` — **USB3-only**, skipped on USB2 `[SRC] halmac_init_8822b.c:962`
- USB RXDMA / aggregation mode `[SRC] halmac_usb_88xx.c:48,123,432`
- bulk-OUT size + EP/queue layout `[SRC] rtl8822bu_halinit.c:410-437` — may change the TX bulk-OUT EP (M8)

NOT covered by a USB2 capture: the USB2 intf-phy writes (`0xFE40-42`) are gated by the empty
`usb2_phy_param_8822b` table, not link speed — dead on 8822b regardless. When porting the
branches above, the USB3 side is gate-verified; the USB2 side stays source-ported-but-uncaptured
until a USB2 capture exists.

## Cleanroom rules

- Do **not** open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` — the
  mainline rtw88-derived driver, its shared base, and its tooling. Reading them produces a
  hybrid; no `_dkms` port imports `rtw88_base` (the AU ports use `rtl88xxau_base` instead).
  (The shared gate *engine* `scripts/rtw88_pcap_replay.py` is fine — family tooling used by
  every `_dkms` recipe, not driver code.)
- This is the **HALMAC + PHYDM (ODM)** vendor stack — port it from the vendor source.
- Sanctioned references: the vendor source (below), the sibling `chips/rtl8812au_dkms/`
  (closest: 2T2R 11ac), `rtl8821au_dkms`, `rtl8814au_dkms`, and their `scripts/*_dkms/`
  recipes. The AU family shares `chips/rtl88xxau_base/`; 8822b is a different HAL, so expect
  new chip-local modules (and possibly a new shared base), not reuse of that one.

## Why this port

Biggest win of the four Realtek 11ac DKMS re-ports — A/B baseline **8 → 29 APs (3.6×)**
(`planning/PORTING.md` § "Cleanroom DKMS re-ports"); the last one pending (8812/8814/8821
`_dkms` done). The gain is the full HALMAC/PHYDM calibration (IQK/DPK/CCK-PD/power-by-rate)
the vendor stack runs.

## Hardware / provenance

- Card: TP-Link Archer T3U Plus v1, `2357:0138`, CUT_D, MP, 2T2R, dual-band (in the DKMS
  `supported-device-IDs`). Windows: Zadig→WinUSB. Linux: unbind kernel driver.
- Vendor: morrownr `rtl88x2bu` 5.13.1 (Realtek 20210702 + community).
  - source: `usb_dumps_new/captures_rtl88x2bu/driver-source/` — HALMAC `hal/halmac/`, 8822b HAL
    `hal/rtl8822b/`, PHYDM/ODM `hal/phydm/`, efuse `hal/efuse/`.
  - tarball: `usb_dumps_new/driver-sources/rtl88x2bu-5.13.1.tar.xz`.
- DKMS cold-boot captures (byte-diff ground truth): `usb_dumps_new/captures_rtl88x2bu/`
  `capture-1/2/3.pcap` + `_logs/` (driver.log, `iw.log` with per-channel `set channel`
  windows, airodump, usb-topology). Driver `rtl88x2bu`, monitor, hops 2.4 then 5 GHz.
- Mainline A/B baseline (tie-or-beat target, not a port reference): `usb_dumps/
  captures_rtw88_8822bu/` + `usb_dumps_new/captures_rtw88_8822bu/`.

## verify_pcap — the faithfulness gate

- `scripts/rtl8822bu_dkms/verify_pcap.py` — capture parse + coverage audit done; bring-up call
  sequence is a TODO. Registered: `uv run python scripts/verify_pcap.py rtl8822bu_dkms`.
- Template: `scripts/rtl8812au_dkms/verify_pcap.py` (gate) + that dir's `verify_channels.py`
  (per-hop `iw set channel` byte-diff). Build a sibling `verify_channels.py`.
- The shared engine `scripts/rtw88_pcap_replay.py` feeds recorded reads back so RMWs / EFUSE /
  any live search reproduce; every write/bulk packet is checked. If the port is dev-centric
  (register IO via `dev.ctrl_transfer`, FW via `dev.write(ep)`), wrap `ReplayTransport` in a
  thin `ReplayDev` shim.
- Run against capture-1/2/3 (Post-Port Checklist #3).

## Milestones (subject to change — the vendor source + capture set the real order)

Port tiny-first, gate every milestone. The agent completes through *wiring* TX; the human
fires live TX (Hand-off).

| M | Scope | HW-verifiable signal |
|---|-------|----------------------|
| M0 | Enumerate + claim + transport + chip-ID reads. Confirm USB topology from the capture. | control plumbing works; chip-id matches; gate green |
| M1 | MAC power-on (HALMAC power sequence + LLT/autoload). | power-on regs byte-for-byte |
| M2 | Firmware upload (HALMAC iDDMA) + FW-ready ACK. Extract FW from pcap, byte-verify vs `linux-firmware/rtw88/`, ship in `assets/`. | FW-ready bit; byte-for-byte |
| M3 | MAC init for RX (TRX enable, RX filters, DRVINFO/PHYSTS). | `REG_CR` TRX bits; byte-for-byte |
| M4 | BB + RF init (PHYDM config tables) + EFUSE read (rfe/board/xtal/tx-power). | tables byte-for-byte; EFUSE decode matches |
| M5 | RF calibration as the vendor runs it on this boot (IQK/DPK/LCK/TSSI — only what's in the capture, reproduced). | byte-for-byte incl. any live search |
| M6 | Channel tune 2.4 + 5 GHz (per-hop). Build `verify_channels.py`. | every `iw set channel` window byte-diffs (incl. band crossings + DFS) |
| M7 | PHYDM dynamics (DIG + CCK-PD) + monitor-mode RX filter. | clean 2.4 GHz beacons, ≥8–10/s, ~29 APs across a hop (beat the A/B 3.6×) |
| M8 | TX descriptor + `inject_frame` wiring. Build `deauth_hw.py`. Agent does not live-inject. | TX byte-diff vs any captured injector, else behavioural via `deauth_hw.py` (human) |
| M9 | Warm-reattach: detect warm (FW-ready + TRX up), skip bring-up, light reattach + bulk-IN smoke test; surface "please replug" if the pipe is wedged. | relaunch resumes RX; wedged pipe → clear message |
| M10 | `driver.py` (WlanDriver protocol) + `manager.py` under `WIFIT3_RTL8822` + RxReaderThread (start before RX-enable) + DIG/CCK-PD ticked off the loop. | clean RX through the app; gate-faithful |
| M11 | A/B vs the mainline baseline (run via env var) + endurance soak (≥30 min, both bands). | tie/beat it on breadth+stability → flip default |

Open questions to resolve from the source (don't guess): does the 8822b HAL reuse
`rtl88xxau_base` or need a new shared base; HALMAC vs legacy efuse path; the morrownr
`CONFIG_*` build flags (deduce from the bytes); the monitor RCR value (a vendor/airmon tail
may not deliver beacons into our RX pipeline — sibling ports re-open RCR after the tail).

## Acceptance: Post-Port Checklist

Run `planning/PORTING.md` § "Post-Port Checklist" before declaring done — gate-green is
necessary, not sufficient. Agent runs 1–6 and reports; 7 is the human's:

1. Waiver review — init + airmon reproduce single-cursor, zero waived ops (only a different
   program's traffic is a legitimate waiver).
2. Skip audit — every kernel/vendor branch we don't emit is a marked `# TODO untestable: <why>`
   genuine no-hardware skip; classify every leaf faithful/hardcoded/omitted/N-A.
3. Capture coverage — gate PASSES on capture-1/2/3 (same silicon confirmed).
4. TX byte-diff vs the captured injector (if any; else `deauth_hw.py` is the only TX check).
5. Async producers — enumerate the vendor's periodic threads (DIG/CCK-PD/link-tuner/thermal);
   dispatch the ones that fire in monitor.
6. Recalibration cadence — per-hop recal matches the vendor's `config_channel`; per-hop HW lock
   holds so a cancelled tune can't strand the chip on a stale channel.

## Hand-off to the user

- TX is the human's trigger; the agent never live-injects. Build `scripts/rtl8822bu_dkms/
  deauth_hw.py` modelled on `scripts/rtl8812au_dkms/deauth_hw.py`: targeted unicast `--client`
  only, `--dry-run` default, watches RX for the target's EAPOL. Target MACs stay on the
  terminal — never commit them.
- Post-Port Checklist #7: hammer it in-app — alternate targets, hop hard, switch channels,
  replug mid-run, soak, fire the live attacks. Hunt the stale-channel hop and any wedge.

## Working style

- Do it inline in the main session. Don't over-decompose into subagents — past ports show it
  slows things down and a port usually leaves 50–70 % context free. Use agents only for
  genuinely parallel independent search.
- Gate each milestone before any hardware/behavioural debugging.
- Commit each milestone (stage only your files; no AI-authorship trailer).
- Keep this doc current with `[SRC]`/`[WIRE]` citations.
- Reference: `planning/PORTING.md` (recipe, hardware-verifiable milestones, Post-Port
  Checklist, the cleanroom-re-port workflow).
