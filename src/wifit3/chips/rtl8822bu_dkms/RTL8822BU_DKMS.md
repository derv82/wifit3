# RTL8822BU — vendor/DKMS port (playbook)

> **STATUS: not started.** This is the start-here playbook for the cleanroom DKMS
> re-port. Read it, then begin at M0. Promote sections from plan to ground truth with
> `[SRC]`/`[WIRE]` citations as facts get confirmed; by the end this should read like
> `chips/rtl8812au_dkms/RTL8812AU_DKMS.md`.

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
