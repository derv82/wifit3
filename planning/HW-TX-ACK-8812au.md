# HW TX-ACK / auto-retransmit on RTL8812AU (DKMS port) — recon + plan

**Status:** RECON COMPLETE / not implemented. Captured 2026-07-11.
**Goal:** make the chip hardware-ACK-track and auto-retransmit our *injected* frames (like a real
associated STA), and read back per-frame TX success — the RTL analogue of an mt76 `MT_TX_STAT_FIFO`
read. Motivation: the distant-AP failure mode is *our TX* dropping, which auto-ACK (an RX lever)
cannot fix.

> **⚠ PIVOTAL UNKNOWN (Gap #1): the premise may already be true.** Our unicast fake-txdesc has no
> "no-ACK" bit and falls back to the global `REG_RETRY_LIMIT = 0x3030` (48). ACK+retransmit is
> automatic for unicast on this chip. So the HW may **already** retransmit our dropped TX — the
> genuinely missing piece may be only the **readback (TX report)**, not the retransmit. Resolve this
> FIRST (needs an on-air test + a lossy link); it could make 80% of the plan below unnecessary.

> **⚠ MEASUREMENT CONSTRAINT:** the ACH sees ~0 frame loss even at −41 dBm through walls (RX-ACK lab,
> 2026-07-11), so **no TX-reliability improvement is measurable on this card/link** without an
> artificially lossy link (drop the AP's TX power / attenuate / use a weaker card). The TX-report
> *instrument* (Milestone 1 below) is still useful without loss — it's how we'd observe retry counts.

---

## 1. How inject/TX works in our port today

- **Entry:** `Rtl8812auDkmsDriver.inject_frame` — `chips/rtl8812au_dkms/driver.py:268-294`. Derives the
  group bit from `frame_bytes[4] & 0x01` (`:290`), calls `build_mgmt_txdesc(len, bmc=…)`, bulk-OUTs
  `desc+frame` on EP 0x02. **`use_no_ack` is accepted but IGNORED** ("minimal descriptor uses the
  HW-default ACK/retry policy", `driver.py:284-285`).
- **Descriptor builder:** `build_mgmt_txdesc` — `chips/rtl88xxau_base/tx.py:56-83` (1:1 port of vendor
  `rtl8812a_fill_fake_txdesc`). Sets: FIRST/LAST_SEG, OFFSET=40, PKT_SIZE, BMC (from addr1 group bit),
  OWN, QUEUE_SEL=QSLT_MGNT(0x12), RATE_ID=RATEID_IDX_B(8), HWSEQ_EN, USE_RATE, TX_RATE=DESC_RATE1M(0),
  checksum (XOR of first 32 B).
- **NOT set (the crux):** MACID (`b4[0:7)`) left **0** (unregistered slot); RETRY_LIMIT_ENABLE /
  DATA_RETRY_LIMIT never set (→ global `REG_RETRY_LIMIT=0x3030` from `mac.py:156`); **SPE_RPT** (TX-report
  request) never set → no per-frame feedback requested.
- **FW TX-report engine already armed at init:** `mac.py:270` `REG_FWHW_TXQ_CTRL+1 = 0x0F` ("enable Tx
  report") and `mac.py:272` `REG_TX_RPT_TIME = 0x3DF0` (from `usb_halinit.c:1653/1659`). We just never
  ask for a report per-frame.
- **`enter_active_monitor` (`driver.py:296-302`) is a DIFFERENT mechanism** — rewrites `REG_MACID=0x610`
  (the card's OWN MAC, `monitor.py:57-61`) so the card **ACKs frames sent TO it** (RX-side). Orthogonal
  to tracking ACKs for frames we TRANSMIT.
- **RX-side gap:** `iter_frames` (`chips/rtl88xxau_base/rx.py:61-86`) **skips every `rpt_sel` packet**
  (`:76`; `rpt_sel = dw2 bit28`, `:52`). C2H FW reports (how a TX report is delivered) are dropped today.
- **Transport:** `rtl88xxau_base/transport.py` has only `read/write{8,16,32}` — **no H2C / HMEBOX helper.**

---

## 2a. Vendor TX descriptor — the ACK/retransmit fields

Field macros: `include/rtl8812a_xmit.h`, the `SET_TX_DESC_*_8812` accessors (`:199-291`) — authoritative
(used by both `fill_fake_txdesc` and `update_txdesc`):

| field | macro | offset:bits |
|---|---|---|
| MACID | `:212` | `+4 [0:7)` |
| RATE_ID | `:217` | `+4 [16:21)` |
| SPE_RPT (TX-report request) | `:230` | `+8 bit19` |
| USE_RATE | `:240` | `+12 bit8` |
| DISABLE_FB (no rate fallback) | `:242` | `+12 bit10` |
| NAV_USE_HDR | `:246` | `+12 bit15` |
| TX_RATE | `:253` | `+16 [0:7)` |
| RETRY_LIMIT_ENABLE | `:256` | `+16 bit17` |
| DATA_RETRY_LIMIT | `:257` | `+16 [18:24)` |
| BMC | `:202` | `+0 bit24` |
| HWSEQ_EN | `:288` | `+32 bit15` |

- **Current inject template:** `rtl8812a_fill_fake_txdesc` — `hal/rtl8812a/rtl8812a_xmit.c:265-340` — sets
  neither MACID, retry limit, nor SPE_RPT (our port matches it faithfully). `TX_RATE` ← `pmlmeext->tx_rate`.
- **Real associated-STA path (the reference):** `update_txdesc` — `hal/rtl8812a/usb/rtl8812au_xmit.c:42-341`:
  `:111` MACID = `pattrib->mac_id` (every frame); `:112` RATE_ID = `pattrib->raid`; MGNT `:302`
  RETRY_LIMIT_ENABLE=1, `:304/306` DATA_RETRY_LIMIT = 6 (retry_ctrl) or **12** default; `:311-312` (data
  `:260-261`) **SPE_RPT = 1 iff `pxmitframe->ack_report`** (under `CONFIG_XMIT_ACK`) — the bit that makes
  the FW emit a TX report.
- **No explicit "disable ACK" bit.** ACK is implicit for unicast; group frames (BMC=1) get none. Retransmit
  is HW-automatic, driven by (a) addr1 unicast + (b) the retry limit — NOT a no-ack flag we clear.
- **Trap:** `rtl8812a_xmit.h` also has an OLD bare-`#define` block + `struct txdescriptor_8812` (`:37-195`)
  whose offsets **disagree** with the SET macros (e.g. `RTY_LMT_EN` under `/* OFFSET 20 */` `:78-79` /
  struct `:163-169` vs the macro's offset-16-bit-17 `:256`). The struct/bare-defines are unused by the
  8812 USB TX path. **Use the SET-macro offsets** (our port already does).

---

## 2b. WCID/MACID / media-status setup

- **Register a peer macid:** `rtw_hal_set_FwMediaStatusRpt_cmd` — `hal/hal_com.c:4607-4694`, builds+sends an
  H2C (`:4634`): id `H2C_MEDIA_STATUS_RPT = 0x01` (`include/hal_com_h2c.h:25`), len 3/4 (`:144-146`). Parm
  (`:264-270`): `byte0 bit0 = opmode`(1=connect), `bit1 = macid_ind`, `[4:8) = role`(1=STA,2=AP,4=GO),
  `byte1 = macid`, `byte2 = macid_end`. Infra STA → AP peer registered `opmode=1, role=AP, macid=<slot>`.
- **MSR / net-type:** `HW_VAR_MEDIA_STATUS` → `rtw_hal_set_msr` — `hal/hal_com.c:13401-13405` → `:2214-2240`
  writes the port-0 net-type nibble of `REG_CR` (MSR, our `monitor.py:13`). Our monitor tail leaves the port
  at **NOLINK** (`monitor.py:97`).
- **H2C-over-USB (UNPORTED):** `fill_h2c_cmd_8812` — `hal/rtl8812a/rtl8812a_cmd.c:61-146`. Gate on FW-ready;
  poll `REG_HMETFR` (0x1CC, `hal_com_reg.h:154`) `BIT(box)` free via `_is_fw_read_cmd_down` (`:29-48`); write
  high bytes to `REG_HMEBOX_EXT0_8812` (0x1F0, `rtl8812a_spec.h:68`)+`box*2`, then low dword
  `[ElementID | parm[0:3]]` to `REG_HMEBOX_0` (0x1D0, `hal_com_reg.h:155`)+`box*4`; rotate box 0-3. A new
  base-transport `h2c()` helper must reproduce this.
- **Minimal "associated peer" state:** a macid slot for the AP + `H2C_MEDIA_STATUS_RPT` connect for it, then
  the TX-desc MACID field pointing at it. The HW ACK-wait+retransmit for the unicast frame is automatic; the
  macid/media-status gives the FW a rate/accounting context so the **C2H TX report is meaningful + keyed to
  that macid**.

---

## 2c. TX report / ACK feedback readback

- **Producer:** the `SPE_RPT` desc bit → FW emits a **C2H `C2H_CCX_TX_RPT = 0x03`** (`hal/hal_com_c2h.h:54`).
- **Delivery over USB:** arrives as an RX packet with `RPT_SEL` set → `pkt_rpt_type = C2H_PACKET`
  (`hal/rtl8812a/rtl8812a_rxdesc.c:46-47`); body handed to `rtw_hal_c2h_pkt_pre_hdl` (`hal/hal_com.c:1225-1245`
  via `usb_ops_linux.c:178-191`).
- **C2H header** (`hal/hal_intf.c:1261-1277`): `id=buf[0]`, `seq=buf[1]`, `payload=buf+2`, `plen=len-2`.
  `C2H_CCX_TX_RPT` is handle-directly (`:1402-1418`) → parsed inline in RX context (suits our RX reader thread).
- **Verdict:** `c2h_handler_8812a` (`rtl8812a_cmd.c:605-623`) routes id 0x03 to `C2HTxFeedbackHandler_8812`
  (`:591-603`): `if (RETRY_OVER | LIFE_TIME_OVER) -> FAIL else -> SUCCESS (ACKed)`.
- **TX-RPT payload** (`include/rtl8812a_cmd.h:134-141`, from `payload`): `payload[0]`: QUEUE_SELECT`[0:5)`,
  PKT_BROADCAST bit5, **LIFE_TIME_OVER bit6**, **RETRY_OVER bit7**; `payload[1]`: **MAC_ID**`[0:8)`;
  `payload[2]`: **DATA_RETRY_CNT**`[0:6)` (retransmits it took); `payload[3:5]`: QUEUE_TIME (256 µs units);
  `payload[5]`: FINAL_DATA_RATE. (The `REG_TX_RPT_CTRL` BIT1/5 enable at `hal_com.c:5071-5085` is 8188E-only;
  8812's enable is the already-ported `REG_FWHW_TXQ_CTRL+1=0x0F` + `REG_TX_RPT_TIME`.)

---

## 3. Concrete minimal plan

### A. TX-descriptor field diff (current → target) in `build_mgmt_txdesc` — all within first 32 B, write BEFORE the checksum call (`tx.py:82`)

| field | offset:bits | current | target |
|---|---|---|---|
| MACID | `+4 [0:7)` | 0 | peer slot (e.g. 1) |
| RATE_ID | `+4 [16:21)` | 8 | keep 8 (or peer raid) |
| SPE_RPT | `+8 bit19` | 0 | **1** (request TX report) |
| RETRY_LIMIT_ENABLE | `+16 bit17` | 0 | **1** |
| DATA_RETRY_LIMIT | `+16 [18:24)` | 0 (global) | **12** (or 6) |
| BMC | `+0 bit24` | from addr1 | keep 0 — unicast only |
| DISABLE_FB | `+12 bit10` | 0 | optional 1 (pin rate) |

Wire `use_no_ack`: `True` → current fake-txdesc (broadcast deauth, no report); `False` → new ACK-tracking desc.

### B. State to establish first (once)
1. Add `h2c(element_id, parm_bytes)` to `rtl88xxau_base/transport.py` (port `fill_h2c_cmd_8812`: HMETFR poll →
   HMEBOX_EXT0 → HMEBOX_0, box rotation).
2. Reserve a macid slot (e.g. 1) for the AP; send `H2C_MEDIA_STATUS_RPT` connect (`opmode=1, role=AP, macid=1`).
3. Likely set the port net-type out of NOLINK (STA/AP) via MSR for the ACK-tracked-TX duration (gap #2).
4. Possibly set `REG_MACID`(0x610) to the injected source (TA) — what `enter_active_monitor` already does (gap #4).

### C. Readback
Extend `iter_frames` so `rpt_sel` packets are surfaced (not dropped): expose the C2H body; when `body[0]==0x03`
decode the TX-RPT → `(macid, acked, retries)`. Correlate to the injected frame by MAC_ID (+ HW seq if tracked).

### Reuse
- **Reusable:** FW TX-report enable (already `mac.py:270-272`); C2H-direct-handle path is inline-friendly for the RX thread.
- **New:** H2C/HMEBOX helper; macid+media-status registration; TX-desc MACID/retry/SPE_RPT; C2H branch in the RX walk.
- **`enter_active_monitor` ≠ this** — it's RX-side (card ACKs frames TO it); may *additionally* be needed so the MAC
  accepts the returning ACK addressed to our TA (gap #4), but doesn't itself give TX-side ACK tracking.

---

## 4. Documentation gaps (need a pcap or on-air experiment)

1. **Is the premise even true?** Unicast injects already fall back to global retry (48) with no no-ACK bit → HW may
   ALREADY retransmit; the missing piece may be only the readback. No TX/air capture exists (all captures are
   cold-boot RX). **Biggest unknown — measure first.**
2. **NOLINK/monitor MSR gating** — whether the MAC does post-TX ACK-wait+retransmit while net-type is NOLINK
   (`monitor.py:97`) is unprovable from source. If it gates, switch net-type for TX.
3. **Does ACK-detection require `REG_MACID` == injected TA?** The ACK's RA = our source; if the MAC filters ACKs by
   `REG_MACID`, we must set it to the TA (reuse `enter_active_monitor`). Unclear.
4. **Does the FW need an add-sta / RA-info H2C before honoring media-status + emitting TX reports for a macid?** The
   vendor path does `rtw_alloc_macid` + full RA setup; whether a bare media-status for a hardcoded slot suffices is untested.
5. **Struct-vs-macro offset disagreement** for RETRY_LIMIT_ENABLE — resolved to the SET macro (offset 16); byte-check
   the first real descriptor.
6. **H2C over USB is unported/unverified** — HMEBOX box-rotation, bFWReady gating, HMETFR poll need porting + live confirmation.
7. **C2H reception under our monitor RCR/RXFLTMAP is unverified** — the RX walk drops `rpt_sel` today; C2H may need admitting.
8. **`use_no_ack` is a no-op today** (`driver.py:284`) — the plan repurposes it as the selector between the two descriptor shapes.

**Net:** the field change is small (5 desc bits + checksum), but only meaningful once (a) an H2C helper +
macid/media-status exist, (b) the RX walk surfaces C2H TX reports, and (c) an on-air test settles gaps #1-#4.

---

## Suggested milestones (methodology: tiny, verify each — but NOTE: no reference pcap exists for H2C/TX-report, so these verify ON-AIR, not against a capture)
- **M1 — TX-report visibility instrument:** H2C helper + one macid via media-status + SPE_RPT/MACID/retry on a
  unicast inject + C2H TX-RPT decode in the RX walk. Outcome: inject a unicast frame and READ BACK acked + retry_cnt.
  Resolves gaps #1/#4/#7 empirically. **This is the measurement tool — build it first; it's useful even before a lossy link.**
- **M2 — confirm/enable auto-retransmit:** with M1's instrument, attenuate the link and confirm retry_cnt climbs +
  delivery holds; tune DATA_RETRY_LIMIT.
- **M3 — Protocol/interface:** once proven on 8812au, lift to the driver Protocol (`inject_frame(wait_for_ack=…)`) and
  port per-driver.

---

## Cheaper alternative: software (RX-side) ACK detection — aireplay-style [RECOMMENDED POC]

Recon 2026-07-11. The distant-drop problem is "did our TX reach the AP." The AP hardware-ACKs any unicast
frame it receives (association-independent); in monitor mode we can just **RECEIVE that ACK — RX-side, NOT
STA-only** (this is how aireplay counts ACKs). Much cheaper than the HW port above.

**Already in place:** RCR admits the control-frame class (`RCR_ACF` bit12 in `RCR_MONITOR_VALUE=0x9000382F`,
`monitor.py:17`, `driver.py:192-193`); `RCR_AAP` (bit0) = promiscuous, so ACKs to our forged MAC are admitted;
`iter_frames` (`rtl88xxau_base/rx.py:61-86`) already passes a 10-byte ACK (only drops crc/icv/rpt_sel).

**What blocks ACKs today (two gates):**
- **RXFLTMAP1 subtype filter** — `mac.py:149` writes `0x0420`; `set_monitor_mode` ORs BIT8 → `0x0520`. **ACK is
  control subtype 13 (BIT13=0x2000), not set → filtered before the bulk-IN pipe.**
- **Parser drops control frames** — `packet.py:136` (only MGMT/DATA), `:364` rejects `<24` B; driver forwards only
  parsed frames (`driver.py:233-235`), so the existing rx-callback fan-out never sees an ACK.

**Minimal change:** `RXFLTMAP1 |= BIT(13)` — scope to bit 13, NOT `0xffff` (an over-broad ACK-flood measurably cost
RX on rtl8188eus, `RTL8188EUS_DKMS.md:217-222`). Toggleable, in the post-gate monitor tail (like the RCR re-open at
`driver.py:192-193`). RCR needs no change.

**POC hooks:**
1. `enable_ack_rx(t)` — the one register write, toggleable.
2. Raw ACK tap in `driver._dispatch` (`driver.py:232-235`), BEFORE parse: `if len(frame)==10 and frame[0]==0xD4:`
   record `frame[4:10]` (RA) + ts; hand to the loop via `call_soon_threadsafe` into a last-ACK-per-MAC map / Event.
3. `inject_frame(wait_for_ack=…)` — repurpose the ignored `use_no_ack` (`driver.py:283-284`): arm the watcher for the
   frame's source MAC, bulk-OUT, wait a short window for a matching ACK, return bool.
4. Wire conditional resend in `WlanTransport.send`/`_send_until` (`association.py:108-111,217-224`) + `interface.send_raw`.
   **Safe:** worst case (RX-missed ACK) degrades to today's unconditional resend — no regression.

**Do NOT use `enter_active_monitor`** (`driver.py:296-302`) — that's the AP-side auto-ACK the WPS lab found harmful.
This observes the AP's ACK to us; it changes nothing about the AP's retransmit.

**Milestones:**
- **A1 — prove detection — ✅ PROVEN 2026-07-11 (8812au):** enable BIT13 + raw ACK tap; inject during a normal WPS
  attempt; confirm we RX ACKs with `RA == our_mac`. Result: **544 ACKs to our MAC / 863 on channel**, in pure monitor
  mode — settles the on-air unknown (BIT13 does land ACKs under the monitor RCR). Off by default; `wps_lab --ack-detect`.
- **A1.5 — conditional resend (immediate win A1 unlocks):** the association layer blind-resends every 0.4 s
  (`association.py:217-224`); with the ACK signal, resend only when *no* ACK was seen → cut the on-air chatter (this run
  injected enough to draw 544 ACKs) and speed each attempt. No loss regime needed to benefit.
- **A2 — conditional resend + measure benefit:** needs a LOSSY link. Resend only on no-ACK; measure delivery/latency
  vs blind resend.

**Cost vs the HW port:** 1 register bit + a raw tap, vs unported H2C mailbox + macid/media-status + C2H TX-report
decode. Gives coarse "an ACK appeared" (ms-scale software resend), not per-frame `retry_cnt` / SIFS-fast HW
retransmit — but ~90% of the distant-AP value.
