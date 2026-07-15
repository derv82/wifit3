# ACK-state + `use_no_ack`: 22-driver findings

**Compiled from a per-driver-family fan-out (10 research subagents, one schema each).**
This is the research deliverable for the brief in `ACK-STATE-DEEP-DIVE.md`. It records what
each driver *actually does today*, surfaces every divergence, and stops there. It does **not**
propose the unification — that is the user's call. Each divergence below is a decision item.

## All 22 read — confirmation

Every driver's `{driver.py, tx.py, rx.py, constants.py}` (plus shared bases + mac/monitor
helpers where the logic lived there) was read. No blank cells.

| # | Driver | Batch |
|---|--------|-------|
| 1 | ar9271_v2 | Atheros ath9k_htc |
| 2–3 | mt76x0u, mt76x2u | MediaTek mt76 |
| 4 | mt7921au | MediaTek connac2 |
| 5–6 | rt2500usb, rt2800usb | Ralink read32/write32 |
| 7–10 | rt3070, rt5370, rt5372, rt5572 | Ralink register_read/write |
| 11 | rtl8187 | Realtek legacy |
| 12–13 | rtl8188eus, rtl8188eus_dkms | Realtek 8188e |
| 14–16 | rtl8812au, rtl8812au_dkms, rtl8814au_dkms | Realtek rtl88xxau/rtw88 |
| 17–19 | rtl8821au, rtl8821au_dkms, rtl8821cu_dkms | Realtek rtl88xxau |
| 20–22 | rtl8822bu, rtl8822bu_dkms, rtw88_8814au | Realtek rtw88 |

## What is uniform across all 22 (no decision needed)

These held everywhere — confirmed, not assumed:

- **`inject_frame` signature** — `async def inject_frame(self, frame_bytes, use_no_ack=True, wait_for_ack=0.0, max_resends=0) -> bool`. Identical in all 22.
- **`ta = frame_bytes[10:16]`** — plain Addr2/TA slice, no QoS/HT-Control adjustment anywhere. Some drivers read it from a seq-stamped copy, but seq-stamp only touches bytes 22–23, so the TA value is identical regardless. No driver mishandles it.
- **`_our_tx_macs.add(ta)`** — guarded by `if self._ack_detect_on and ta is not None:` in every driver. (One nuance: `rtl8821cu_dkms` skips the add on its synchronous no-running-loop path, `driver.py:347`.)
- **`_await_ack`** — byte-identical to the baseline in all 22: polls `_ack_last_ts.get(ta,0.0) > since` until `since+window`, `asyncio.sleep(0.001)` between checks. Zero deviation.
- **ACK-frame recognition + RA offset** — every driver checks `len(mpdu)==10 and mpdu[0]==0xD4` and reads `ra = mpdu[4:10]`. **RA offset within the MPDU is 4 everywhere.** The HW-descriptor strip length differs per family (see table), but each driver's RX iterator strips it *before* the ACK check runs, so the concern in the brief — "a driver that gets the RA offset subtly wrong would under-count ACKs" — did not materialize. This axis is clean.
- **The 7 state fields** — `_ack_detect_on, _our_tx_macs, _ack_sightings, _all_acks_seen, _ack_last_ts, _tx_frames, _tx_unacked` — present and correctly named in all 22.

## Corrections to the brief's baseline (stale claims)

Three baseline claims in `ACK-STATE-DEEP-DIVE.md` are inaccurate against current code:

1. **`_note_ack(ra)` is not a method — anywhere.** The brief describes it as "called from the RX decode." Behaviorally that is true, but **all 22 drivers inline the body** (`_all_acks_seen += 1; if ra in _our_tx_macs: _ack_sightings[ra.hex()] += 1; _ack_last_ts[ra] = now`) directly in their RX dispatch. There is no `_note_ack` function to change — a refactor touches 22 inlined copies. (Consequence: the tap is never independently unit-testable by name.)
2. **`FAKE_MAC` is a capability enum, not a MAC value.** It holds `FakeMacSupport.{NONE, UNIMPLEMENTED, SPOOFABLE}`, not an address. The forged MAC is caller-supplied to `enter_active_monitor(mac, ...)`. There is no fake-MAC address constant in any driver.
3. **"22/22 thread `use_no_ack` into the TX descriptor" is false.** Only 11 drivers wire it to anything; the other 11 (all Realtek except `rtl8821cu_dkms`) accept it and drop it. See Divergence A.

## Per-driver table

`u_n_a` = does `use_no_ack` change the descriptor? · `ack_detect` = enable/disable mechanism · `strip` = bytes before the MPDU · `act-mon` = active-monitor capability/mechanism.

| Driver | u_n_a | destination | ack_detect | strip | act-mon | headline anomaly |
|--------|-------|-------------|-----------|-------|---------|------------------|
| ar9271_v2 | **IGNORED** | none (default True) | SW flag | 52 (HTC+status) | SPOOFABLE / reg-MAC `AR_STA_ID0/1` | dead param; no no-ACK bit exists at all |
| mt76x0u | honored | TXWI `ACK_CTL_REQ` b0 | **REG** `MT_RX_FILTR_CFG` b10 | 36 | SPOOFABLE / reg-MAC `MT_MAC_ADDR_DW0/1` | inject rate hard-fixed 6M; sync bulk-out on loop |
| mt76x2u | honored | TXWI `ACK_CTL_REQ` b0 | **REG** `MT_RX_FILTR_CFG` b10 | 36 | SPOOFABLE / reg-MAC full `mac_setaddr` | asyncio.Lock (not threading.Lock); band-selected rate |
| mt7921au | honored | TXD3 `NO_ACK` b0 | **REG via FW MCU** `RFCR DROP_UNWANTED_CTL` b21 | variable | UNIMPL flag **but implemented** / **firmware-BSS** | flag says UNIMPL yet enter/exit work; variable RX offset |
| rt2500usb | honored | TXD `W0_ACK` (inverted) | SW flag | 0 (RXD trails) | **NONE** (no autoresponder) | frame leads, descriptor trails |
| rt2800usb | honored | TXWI `W1_ACK` b0 | SW flag | 20 / 28 | SPOOFABLE / reg-MAC `MAC_ADDR_DW0/1` | inject holds **no lock** — races `set_channel` |
| rt3070 | honored | TXWI `W1_ACK` b0 | SW flag | 20 | SPOOFABLE / reg-MAC | reference impl (clean) |
| rt5370 | honored | TXWI `W1_ACK` b0 | SW flag | 20 | SPOOFABLE / reg-MAC | monitor AGC tuner; `_dispatch` drops early-return |
| rt5372 | honored | TXWI `W1_ACK` b0 | SW flag | 20 | SPOOFABLE / reg-MAC | clean rt3070 clone |
| rt5572 | honored | TXWI `W1_ACK` b0 | SW flag | 28 (6-word RXWI) | SPOOFABLE / reg-MAC (exit omits u2me=0) | **no seq stamp**; unserialized inject; hardcoded EP |
| rtl8187 | **REWIRED** | retry_count 1 vs 7 (no no-ACK bit) | SW flag | 0 (RX hdr is trailer) | **NONE** (no autoresponder) | `use_no_ack`→retry count; SW seq stamp |
| rtl8188eus | **IGNORED** | none (retry 6 hardcoded) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | **UNIMPL** / none | FCS handling may break `len==10` ACK check — verify on HW |
| rtl8188eus_dkms | **IGNORED** | none (retry 12 hardcoded) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | SPOOFABLE / reg-MAC `REG_MACID` | mixed HW/SW seq stamp |
| rtl8812au | **IGNORED** | none (no retry field) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | **UNIMPL** / none | lineage = `rtw88_base`, not `rtl88xxau_base` |
| rtl8812au_dkms | **IGNORED** | none (no retry field) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | SPOOFABLE / reg-MAC | TX+transport inherited from `rtl88xxau_base` |
| rtl8814au_dkms | **IGNORED** | none (retry **12 hardcoded**) | **SW flag** | 24+drvinfo+shift | SPOOFABLE / reg-MAC `REG_MACID` | ACK tap on **reader thread**; retry=12 baked in |
| rtl8821au | **IGNORED** | none (no retry field) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | **UNIMPL** / none | `band_is_2g` hardcoded True |
| rtl8821au_dkms | **IGNORED** | none (no retry field) | **SW flag** | 24+drvinfo+shift | SPOOFABLE / reg-MAC `REG_MACID` | frozen own copies (base's origin) |
| rtl8821cu_dkms | **HONORED** | `RTS_DATA_RTY_LMT` 0 vs 6 | **SW flag** | 24+drvinfo+shift | SPOOFABLE / reg-MAC `REG_MACID` | **only Realtek honoring `use_no_ack`**; 48B desc; no-loop path skips add |
| rtl8822bu | **IGNORED** | none (no retry field) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | **UNIMPL** / none | RCR 0xf410400f |
| rtl8822bu_dkms | **IGNORED** | none (retry 12 hardcoded) | **SW flag** | 24+drvinfo+shift | SPOOFABLE / reg-MAC `REG_MACID` | own transport w/ 0x4E0 mirror; stale "stub" docstring |
| rtw88_8814au | **IGNORED** | none (no retry field) | **REG** `RXFLTMAP1` b13 | 24+drvinfo+shift | **UNIMPL** / none | ACK-filter in `rx.py`, not `mac.py` |

---

## Divergences menu (the decision items)

### A. `use_no_ack` wiring — the biggest split (problem #2)

Three distinct behaviors. Only one caller ever passes a non-default value
(`attacks/auth_assoc.py::WlanTransport.send`, `not self.ack`), so on the "IGNORED" group that
caller is currently a **silent no-op**.

- **A1 — Honored via a real no-ACK / ACK-request descriptor bit (9 drivers):**
  `mt76x0u`, `mt76x2u` (TXWI `ACK_CTL_REQ`); `mt7921au` (TXD3 `NO_ACK`); `rt2500usb` (TXD `W0_ACK`, inverted); `rt2800usb`, `rt3070`, `rt5370`, `rt5372`, `rt5572` (TXWI `W1_ACK`). All Ralink + MediaTek. HW expects/retransmits an ACK when `use_no_ack=False`.
- **A2 — Honored via retry-limit, not a no-ACK bit (2 drivers):**
  `rtl8821cu_dkms` — `retry_ctrl = not use_no_ack` → `RTS_DATA_RTY_LMT` = 0 or 6 (`tx.py:91-92`). `rtl8187` — `use_no_ack` picks `retry_count` = 1 vs 7 in `build_tx_hdr` (`driver.py:352`→`tx.py:90`); there is no no-ACK bit on the 8187L L-path.
- **A3 — Ignored / dead parameter (11 drivers):** every remaining Realtek —
  `ar9271_v2`, `rtl8188eus`, `rtl8188eus_dkms`, `rtl8812au`, `rtl8812au_dkms`, `rtl8814au_dkms`, `rtl8821au`, `rtl8821au_dkms`, `rtl8822bu`, `rtl8822bu_dkms`, `rtw88_8814au`. The param is accepted (some docstring it explicitly), never read, never reaches the builder.
  - Sub-split inside A3: some hardcode a HW retry-limit on **every** injected frame regardless (`rtl8188eus`=6, `rtl8188eus_dkms`/`rtl8814au_dkms`/`rtl8822bu_dkms`=12), others set **no** retry field so HW-default applies (`ar9271_v2`, `rtl8812au`, `rtl8812au_dkms`, `rtl8821au`, `rtl8821au_dkms`, `rtl8822bu`, `rtw88_8814au`).

> The brief's hypothesis — move the decision to interface-level state (are we active-monitor-armed for this TA) — is consistent with the fact that the descriptor bit is currently the *only* place the policy lives on A1, is a retry proxy on A2, and is inert on A3. This confirms the layering question is real for all 22; the per-driver mechanics of *where the bit would come from* differ across the three groups.

### B. `enable_ack_detect` / `disable_ack_detect` — register write vs software flag

The known-biggest split, mapped for all 22. Software-only drivers rely on the monitor RX
filter already admitting ACK control frames (set during monitor bring-up), so no per-arm
register write is needed. Register-write drivers must flip a bit because their monitor filter
leaves ACK subtypes restricted.

- **B1 — Register write (10 drivers):**
  - Realtek `RXFLTMAP1` (0x06A2) bit 13: `rtl8188eus`, `rtl8188eus_dkms`, `rtl8812au`, `rtl8812au_dkms`, `rtl8821au`, `rtl8822bu`, `rtw88_8814au`.
  - MediaTek `MT_RX_FILTR_CFG` (0x1400) bit 10 (clear-to-admit): `mt76x0u`, `mt76x2u`.
  - `mt7921au` — clears `RFCR DROP_UNWANTED_CTL` (bit 21) **through a firmware MCU `SET_RX_FILTER` command**, not a raw register write.
- **B2 — Software-only flag, no register I/O (12 drivers):**
  `ar9271_v2`, `rt2500usb`, `rt2800usb`, `rt3070`, `rt5370`, `rt5372`, `rt5572`, `rtl8187`, `rtl8814au_dkms`, `rtl8821au_dkms`, `rtl8821cu_dkms`, `rtl8822bu_dkms`.

> Note the intra-family inconsistency this creates: among **mainline** Realtek, all do the register write; among **dkms** Realtek, `rtl8812au_dkms` writes the register but `rtl8814au_dkms`, `rtl8821au_dkms`, `rtl8822bu_dkms` are software-only (their monitor init already sets `RXFLTMAP1=0xFFFF`). So the mainline/dkms pairs diverge on this axis for 8814/8821/8822.

### C. `_our_tx_macs` add-only / never cleared (problem #1) — universal

Confirmed in every driver: `_our_tx_macs` is a `set`, added to in `inject_frame` (guarded by
`_ack_detect_on and ta is not None`), read in the inlined ACK tap (`if ra in self._our_tx_macs`),
and **never cleared** — `enable_ack_detect` resets `_ack_sightings`, `_ack_last_ts`, and the
counters but deliberately leaves `_our_tx_macs`; `disable_ack_detect` only flips the flag.

The brief's reasoning — one MAC spoofed at a time, ACK correlation is stop-and-wait, so a single
current-MAC value would suffice — **holds against all 22**: no driver adds more than the current
TA per inject, none reads `_our_tx_macs` for anything except the membership test, and the set's
add-only growth is the only reason a spoofed Addr2 (e.g. a deauth's Addr2 = the AP) lingers as a
valid ACK target. The precondition for collapsing the set to a scalar is satisfied uniformly.
(Stated as a fact the brief asked to confirm — not a proposed fix.)

### D. Active-monitor capability + mechanism

- **D1 — Register-MAC (13 drivers):** writes a MAC register so the chip HW-ACKs the forged MAC.
  `ar9271_v2` (`AR_STA_ID0/1`); `mt76x0u`, `mt76x2u` (`MT_MAC_ADDR_DW0/1`); `rt2800usb`, `rt3070`, `rt5370`, `rt5372`, `rt5572` (`MAC_ADDR_DW0/1` + `UNICAST_TO_ME_MASK`); `rtl8188eus_dkms`, `rtl8812au_dkms`, `rtl8814au_dkms`, `rtl8821au_dkms`, `rtl8821cu_dkms`, `rtl8822bu_dkms` (`REG_MACID` 0x0610). *(That's the dkms Realtek set.)*
- **D2 — Firmware-offload BSS (1 driver):** `mt7921au` — `enter/exit_active_monitor` send connac2 UNI `DEV_INFO_UPDATE` (omac) + `BSS_INFO_UPDATE`. No MAC-register write. **Its `FAKE_MAC` flag reads `UNIMPLEMENTED` while the methods are fully implemented** — a flag/code contradiction to resolve.
- **D3 — Not implemented / no autoresponder (8 drivers):**
  `rt2500usb`, `rtl8187` (hardware genuinely has no autoresponder → `FAKE_MAC=NONE`); `rtl8188eus`, `rtl8812au`, `rtl8821au`, `rtl8822bu`, `rtw88_8814au` (`UNIMPLEMENTED`, no enter/exit methods — every **mainline** Realtek). 

> Clean mainline-vs-dkms split on Realtek: mainline = `UNIMPLEMENTED`/no methods, dkms = `SPOOFABLE`/register-MAC. This is the emit side (separate from ACK detection) but part of the same picture per the brief.

### E. Smaller per-driver deviations worth knowing before any refactor

- **`rtl8188eus` (mainline) FCS handling** — neither sets an RCR APPFCS bit nor strips a trailing FCS; it relies on HW delivering `pkt_len` already FCS-free so the `len(mpdu)==10` ACK check matches. If this chip includes FCS in `pkt_len`, the ACK check never fires and every parsed frame carries 4 trailing bytes. Its dkms sibling appends+strips FCS. **Flagged verify-on-HW** — could silently zero ACK counts on mainline 8188eus.
- **`rtl8821cu_dkms` no-running-loop path** — its verify/sync path (`driver.py:344-348`) returns before computing `ta`, so `_our_tx_macs.add` and ACK gating are skipped there. Only matters for the offline verify harness, not live TX.
- **`rtl8814au_dkms` ACK tap runs on the RX reader thread** (`_read_once`), not the event loop `_dispatch` like the other two rtl88xxau drivers — a threading difference in where the inlined note-logic executes.
- **`rt5572` outlier** in the Ralink `register_read/write` family — no sequence stamping (repeated injects can share seq=0), inject not lock-serialized against tuning, hardcoded bulk-OUT endpoint, 6-word (24B) RXWI vs the others' 4-word (16B). `rt5370` adds a monitor AGC tuner and drops the `_dispatch` early-return guard. `rt5372` is a clean `rt3070` clone.
- **`rt2800usb` / `rt5572` inject holds no lock** — can race `set_channel` / link-tuner register I/O on the control endpoint. `rt2500usb`, `rt3070`, `rt5370`, `rt5372` serialize inject under `_io_lock`+`_hw_lock`.
- **`mt7921au` variable RX MPDU offset** — computed per-frame from connac2 RXD group bits, not a fixed strip length. Anyone assuming a constant would be wrong; the ACK check still runs on the correctly-sliced MPDU so RA offset stays 4.
- **Lineage is not uniform inside the Realtek "families":** `rtl8812au` (mainline) uses `rtw88_base`; `rtl8812au_dkms` uses `rtl88xxau_base`; `rtl8814au_dkms`, `rtl8821au`, `rtl8821au_dkms`, `rtl8821cu_dkms` use neither (own copies). `rtl88xxau_base` serves **only** `rtl8812au_dkms`. Any change described as "edit the base" touches at most one driver — the rest are per-driver copies.

---

## Bottom line for the decision

Two problems, both confirmed present in the per-driver ACK machinery:

- **Problem #1 (`_our_tx_macs`)** is *uniform* — same add-only/never-cleared shape in all 22, and the single-scalar simplification's precondition holds everywhere. A change here is 22 near-identical inlined edits (there is no shared method).
- **Problem #2 (`use_no_ack`)** is *not uniform* — it splits 9 (real ACK bit) / 2 (retry proxy) / 11 (dead). Whatever layer the decision moves to has to produce the descriptor bit for the A1 group, a retry value for A2, and can be a pure no-op for A3 (or A3 stays dead).

Cross-cutting inconsistencies the user may want to level (each is a menu item, not a recommendation): **A** the three `use_no_ack` behaviors; **B** register-write vs software-flag ACK admit (incl. the mainline/dkms Realtek split on 8814/8821/8822); **D** active-monitor register-MAC vs firmware-BSS vs unimplemented (incl. every mainline Realtek being UNIMPLEMENTED); plus the **baseline corrections** (no `_note_ack` method exists; `FAKE_MAC` is a capability flag) and the **`rtl8188eus` mainline FCS** question that needs hardware confirmation.
