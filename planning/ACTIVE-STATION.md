# Active Station — HW-ACK a chosen MAC across every chip family

## Why

An ACKed 802.11 conversation (WPS, EAP, future software FakeAP) needs the radio to
**hardware-ACK frames addressed to our (forged) MAC** while in monitor mode. Without it
an ACK-strict AP (Broadcom-class) retransmits each downlink frame to its 802.11 retry
limit and abandons the session — the WPS-PBC-stalls-after-M1 failure. `mt7921au` is
done (commit `8c0bcb9e`); this rolls the same capability to the rest of the fleet.

**Correctness note:** every WPS "green" in `VERIFICATION.md` / `<CHIP>.md` was earned
against *tolerant* APs that proceed without our ACK. Against an ACK-strict AP they were
never validated, so each is a `?` until re-tested with active-station. This plan turns
the `?`s into honest greens (or honest NONEs), family by family.

## The contract (already built for mt7921au — reuse as-is)

- `engine/protocols.py` — `FakeMacSupport` enum (`NONE` / `FIXED_MAC` / `SPOOFABLE`);
  optional `enter_active_monitor(mac, bssid=None) -> assumed_mac` / `exit_active_monitor()`,
  gated via `getattr`/`hasattr` so un-ported drivers stay valid (treated as `NONE`).
- `wlan/interface.py` — `set_fake_mac(mac, bssid=None)` / `clear_fake_mac()` front the
  capability, fold into the forged-MAC registry, return the armed MAC (or `None`).
- `engine/attacks/wps/pbc.py` — arms `set_fake_mac` before the exchange, clears in
  `finally`; `WlanTransport(ack=...)` requests HW retransmit only when armed. PIN can
  adopt the same two calls.

Per-driver work = declare `FAKE_MAC` + implement `enter/exit_active_monitor`. Exit must
**restore the exact monitor baseline** (re-zero / re-point the MAC reg, restore any RCR
bits it flipped) — never invent a teardown.

## Capability matrix

| Family | Driver dirs | Own-MAC register | enter_active_monitor writes | Confidence | FAKE_MAC |
|---|---|---|---|---|---|
| Ralink rt2800 | rt2800usb, rt3070, rt5372 | `MAC_ADDR_DW0/1` 0x1008/0x100c [SRC rt2800lib.c:2046-2065] | MAC + `U2ME_MASK=0xff`; autoresponder already on, monitor already clears `DROP_NOT_TO_ME` | **high** (MAC-only) | SPOOFABLE |
| MediaTek mt76x | mt76x0u, mt76x2u | `MT_MAC_ADDR_DW0/1` 0x1008/0x100c [SRC mt76x02_mac.c:727] | re-point MAC (drivers already write EFUSE MAC) + ensure `AUTO_RSP` | med (verify AUTO_RSP) | SPOOFABLE |
| connac2 | mt7921au | omac via `DEV_INFO` | **done** | confirmed (HW) | SPOOFABLE |
| Realtek rtl8xxxu | rtl8812au, rtl8821au, rtl8188eus (+dkms) | `REG_MACID` 0x0610 [SRC regs.h:789] | **MAC-only** — re-point REG_MACID; the accept-all monitor RCR still HW-ACKs RA==REG_MACID (no RCR flip needed) | **high (proven rtl8812au_dkms)** | SPOOFABLE |
| Realtek rtw88 | rtl8822bu, rtw88_8814au (+dkms) | `REG_MACID` 0x0610 [SRC main.c:925] | same as rtl8xxxu (MAC-only) | **high (proven rtl8822bu_dkms)** | SPOOFABLE |
| Atheros | ar9271 | `AR_STA_ID0/1` 0x8000/0x8004 via WMI reg-write [SRC reg.h:1637] | MAC → `AR_STA_ID0/1` (flags cleared — fine in monitor); ACK matches STA_ID, **not** BSS_ID | **high (proven ar9271)** | SPOOFABLE |
| Realtek rtl8187 | rtl8187 | MAC[0:5] EEPROM-backed [SRC rtl818x.h:17] | — monitor is passive RX, no ACK engine | confirmed | **NONE** |
| Ralink rt2500usb | rt2500usb | MAC_CSR2/3/4 writable [SRC rt2500usb.h:79] | — no hardware autoresponder | confirmed | **NONE** |

**The two NONEs are not a hole** — they're an honest hardware boundary. `set_fake_mac`
returns `None`, the WPS arming path surfaces "this card can't ACK a spoofed MAC; WPS
unavailable," and the attack stops cleanly. Accurate > green.

## Reuse: existing MAC-write helpers (lower risk than fresh writes)

- rtl8188eus_dkms `mac.set_macid`, rtl8812au_dkms `monitor._write_mac_addr`,
  rtl8814au_dkms `monitor._set_macaddr`, rtl8822bu_dkms `mac.set_mac_addr`,
  mt76x0u `mac.mac_setaddr` — re-point these at the forged MAC.
- rt2800usb has `MAC_ADDR_DW0/1` constants; rt3070/rt5372 share `rt2800lib` semantics but
  need the register write added.

## Scope — default drivers only

Active-station lands on each card's **default/recommended** driver (what we ship + test).
Fallback variants (mainline ↔ `_dkms`, env-selectable) stay `FAKE_MAC=UNIMPLEMENTED` by
default (the undeclared default) — **zero work**, and the UX stays honest: "Active Monitor
not implemented for `<chip>` — use `<default driver>`", NOT a false "card can't" (the
silicon can; our fallback driver just didn't). Distinct from `NONE` = hardware genuinely
can't (rtl8187, rt2500usb). Parity follows
recommendation, not existence: a fallback earns active-station only if it ever becomes the
*better* driver for a card. (rtl8812au mainline: never — RF-deaths in 1–5 min; superseded by
the DKMS port.)

## Rollout (phased)

The capability is identical everywhere; the *mechanism* differs per family and is
near-identical within one. So: learn each family once on the wire, then replicate. Tuned
for lean agent context (one driver in head at a time) and minimal card-swaps (one
HW-confirm per distinct mechanism; siblings spot-checked).

**Phase 1 — connac2 proof (DONE).** mt7921au established the contract + experiment loop.

**Phase 2 — one representative per family, test-as-we-go.** Implement one driver → user
fires WPS at an ACK-strict AP → fix on the spot while context is warm → commit → flush,
take the next family. Reps, easiest mechanism first:
1. Ralink rt2800 — `rt2800usb` (MAC-only; autoresponder already on). Cleanest.
2. MediaTek mt76x — `mt76x2u` (already writes the MAC; re-point + AUTO_RSP).
3. Realtek rtl8xxxu — `rtl8812au_dkms` (REG_MACID + the RCR gotcha; most cards ride this).
4. Realtek rtw88 — `rtl8822bu_dkms` (same idea, different reg-write stack).
5. Atheros — `ar9271` (WMI reg-write; settle STA_ID vs BSS_ID).

**Phase 3 — siblings, folded into each family (lead does them inline).** Drivers are
deliberately **anti-DRY — there is NO shared family base.** rt2800usb / rt3070 / rt5372 are
separate implementations with *different transports* (read32/write32 vs register_read/
register_write) and their own monitor/constants modules; many chips also have a mainline +
`_dkms` pair. (Why: a shared core meant a fix for one card had to be re-tested on all and
could regress others.) So a "sibling" is a **separate port of the same mechanism** (same
registers, same U2ME/RCR trick) into its own driver's structure — mechanical, but NOT
copy-paste. The lead does them one at a time — wire → user tests → fix while warm → commit
— NOT fanned out (HW testing is serial; write-and-leave delegation breaks the test-fix
loop). Context stays bounded by the per-driver boundary + the Status board below. Sets:
- rtl8xxxu: rtl8812au, rtl8821au(+dkms), rtl8188eus(+dkms), rtl8814au_dkms
- rtw88: rtw88_8814au, rtl8822bu
- Ralink: rt3070, rt5372
- mt76x: mt76x0u

**Phase 4 — the two NONEs + UX.** Declare `FAKE_MAC=NONE` on rtl8187 + rt2500usb, and wire
the stage-specific UX (enum-gated, one place): WPS-PBC auto-invade → log + toast "card
can't ACK a spoofed MAC, skipping"; WPS-PIN button → modal "This card can't spoof a MAC
for WPS" + [Continue with real MAC] / [Cancel].

**Phase 5 — tighten the contract.** Once every driver declares `FAKE_MAC`, promote it +
`enter/exit_active_monitor` from optional (getattr/hasattr) into the required `WlanDriver`
Protocol (or an ABC). The hasattr hack dies.

Phases 2 and 3 interleave per family — rep on the wire, then its siblings delegated, then
the next family — they are not sequential blocks.

Per rep: implement `enter/exit_active_monitor` + `FAKE_MAC`, pcap-verify the monitor
baseline is unchanged where a gate exists, user fires WPS at an ACK-strict AP. Success =
the post-M1 retransmit storm shrinks and M2..M8 flow.

## Doc truth (do this as each family lands, never preemptively)

When a family HW-confirms against an ACK-strict AP, flip its WPS entry from `?` to green
in `VERIFICATION.md` and note the active-station requirement in its `<CHIP>.md`. rtl8187
/ rt2500usb get an honest "WPS n/a — no spoofed-MAC ACK." No mass red-X churn; the column
fills in as the wire proves each one.

## Status — the source of truth (survives any context reset)

This board + the commits are the durable record; the lead's context is a disposable
cache. A mid-rollout reset resumes from here, not from holding 14 drivers in head.
`[x]` HW-green · `[~]` wired, awaiting HW · `[ ]` not started.

- `[x]` mt7921au — SPOOFABLE — HW-green (commit 8c0bcb9e); **chatty ~120 EAPOLs** (reserved-WCID uplink → no ACK-tracked retransmit). STA_REC would fix it AND unlock per-frame TX-status (the "ACK effectiveness %" feature). Both PAU0F + AXML.
- `[x]` rt2800usb — SPOOFABLE — HW-green (PAU09/RT5572, ~20 EAPOLs, clean)
- `[x]` rt3070 — SPOOFABLE — HW-green (AWUS036NH, ~13 EAPOLs; added write_mac_address)
- `[x]` mt76x2u — SPOOFABLE — HW-green (AWUS036ACM, ~24 EAPOLs, 2.4 GHz; 5 GHz inject broken, see BUGS)
- `[x]` rtl8812au_dkms — SPOOFABLE — HW-green (AWUS036ACH, ~25 EAPOLs; MAC-only, no RCR flip; 5 GHz works too)
- `[x]` rtl8822bu_dkms — SPOOFABLE — HW-green (Archer T3U Plus, ~22 EAPOLs, 2.4+5 GHz; rtw88 stack, MAC-only)
- `[x]` ar9271 — SPOOFABLE — HW-green (AWUS036NHA, ~14 EAPOLs; STA_ID0/1 via WMI, flags-clear OK; needed a tuning fix first)
- `[x]` rtl8814au_dkms — SPOOFABLE — HW-green (AWUS1900, ~25 EAPOLs, 2.4+5 GHz; rtl8xxxu sibling)
- `[x]` mt76x0u — SPOOFABLE — HW-green 2.4 + 5 GHz (AWUS036ACHM, ~24 EAPOLs; needed U2ME-aware writes). WPS+PMKID work both bands; 5 GHz RX is *weak* (separate RF-sensitivity issue, see BUGS)
- `[x]` rtl8188eus_dkms — SPOOFABLE — HW-green (TL-WN722N v2/v3, ~23 EAPOLs, 2.4 GHz; rtl8xxxu sibling)
- `[x]` rt5372 — SPOOFABLE — HW-green (Panda PAU05, ~24 EAPOLs, 2.4 GHz; Ralink sibling, rt3070 twin)
- `[ ]` last sibling: rtl8821au_dkms (mainline/non-dkms variants stay UNIMPLEMENTED per Scope)
- `[x]` rtl8187 — NONE declared (passive monitor, no ACK engine; WPS only limps through un-ACKed, slow/unreliable)
- `[x]` rt2500usb — NONE declared (no autoresponder)
