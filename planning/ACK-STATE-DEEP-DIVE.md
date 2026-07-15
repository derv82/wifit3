# ACK-state + `use_no_ack`: 22-driver deep-dive brief

**For the next agent. This is a research brief — understand the problem completely, do NOT solve it.**

The two remaining BUGS.md items (`_our_tx_macs`, `use_no_ack`) both live in the per-driver
TX/ACK path and touch **all 22 drivers**. Before anyone proposes a fix, we need to *know* —
exactly, per driver — how each one records TX-ACKs and handles `use_no_ack`, including every
place a driver deviates from the others. The user will then rule on which inconsistencies get
unified. You do not propose the unification.

## The anti-goal (why this brief exists)

The failure to avoid: an agent reads a few drivers, assumes the rest match, designs a change,
and then one driver turns out to differ — the design conflicts and the agent spins trying to
reconcile it. So: **read ALL 22, extract the same schema from each, surface every divergence.**
A blank cell in the table below = an unread driver = the exact hole we're trying to close.

## The two problems (verify against current code — this brief may be stale)

**1. `_our_tx_macs` — add-only, never cleared, absorbs spoofed MACs.**
Each driver keeps a `set` of "source MACs we've injected as," added to on every ACK-detected
inject and read in the RX path (`_note_ack`) to decide whether an observed ACK is "ours." It is
never reset by `enable_ack_detect`, and it absorbs *spoofed* Addr2 values (a deauth's Addr2 is
the AP itself), so a real station's ordinary ACK to that AP can be miscounted as delivery of our
injected frame. We only ever spoof one MAC at a time and ACK correlation is stop-and-wait, so a
single current-MAC value would suffice — but confirm that reasoning holds against **every**
driver's actual usage before treating it as settled.

**2. `use_no_ack` lives in the wrong layer.**
`inject_frame(..., use_no_ack=True, ...)` is threaded through all 22 signatures and down into
each chip's TX descriptor (the "no-ACK policy" bit). But the decision — do we let the chip
expect/retransmit an ACK — is interface-level state (are we active-monitor-armed for this TA).
Only one caller passes a non-default value (`attacks/auth_assoc.py::WlanTransport.send`,
`not self.ack`). The deep-dive answers: for each driver, *where* does `use_no_ack` go, what
exactly does it do to the descriptor, and does any driver ignore it or treat it differently.

The two are related: both are the per-driver ACK machinery, so research them together.

## Baseline shape (from this session's work — 20-day staleness rule applies: verify each claim)

Post-revert, every driver **re-implements this inline — there is no shared base class.** The
common shape as of the last Protocol pass:

- `async def inject_frame(self, frame_bytes, use_no_ack=True, wait_for_ack=0.0, max_resends=0)` — 22/22.
- Computes `ta = frame_bytes[10:16]` (Addr2 / TA — who the AP ACKs back to). If
  `self._ack_detect_on and ta is not None:` → `self._our_tx_macs.add(ta)`.
- `use_no_ack` → the chip's TX-descriptor builder (e.g. rt2800usb `_inject_frame(use_no_ack=...)`,
  mt7921au `tx.build_tx(no_ack=use_no_ack)`).
- ACK-wait loop: `for _ in range(max_resends+1): ok = await send_one(); if not (wait_for_ack>0
  and self._ack_detect_on and ta): return ok; if await self._await_ack(ta, t0, wait_for_ack): return True`.
- `_await_ack(ta, since, window)`: poll `self._ack_last_ts.get(ta, 0.0) > since` until
  `since + window`, `await asyncio.sleep(0.001)` between checks.
- `_note_ack(ra)` — called from the RX decode when an ACK control frame is seen (`ra = frame[4:10]`):
  `self._all_acks_seen += 1; if ra in self._our_tx_macs: self._ack_sightings[ra.hex()] += 1;
  self._ack_last_ts[ra] = now`.
- State fields: `_ack_detect_on, _our_tx_macs, _ack_sightings, _all_acks_seen, _ack_last_ts,
  _tx_frames, _tx_unacked`.
- `enable_ack_detect` / `disable_ack_detect`: **chip-specific** — some write a register to admit
  ACK control frames (mt7921au: clear RFCR DROP_UNWANTED_CTL), some are a pure software flag with
  no register write (rt2800usb). **This is the biggest known split — map it for all 22.**
- Active-monitor (`enter/exit_active_monitor`, `FAKE_MAC`) is the *emit* side (chip HW-ACKs a
  chosen MAC): register-MAC radios vs firmware-offload (connac2). Separate from ACK *detection*
  but part of the same picture — record it too.

## The 22 drivers (each has its own `chips/<name>/driver.py`)

ar9271_v2, mt76x0u, mt76x2u, mt7921au, rt2500usb, rt2800usb, rt3070, rt5370, rt5372, rt5572,
rtl8187, rtl8188eus, rtl8188eus_dkms, rtl8812au, rtl8812au_dkms, rtl8814au_dkms, rtl8821au,
rtl8821au_dkms, rtl8821cu_dkms, rtl8822bu, rtl8822bu_dkms, rtw88_8814au.

## Per-driver extraction schema (fill EVERY field for EVERY driver)

From `chips/<name>/{driver.py,tx.py,rx.py,constants.py}`:

1. **inject_frame** — `file:line`; exact signature; how `ta` is computed (always `[10:16]`? any
   QoS/HT-Control adjustment?); the exact condition + line where `_our_tx_macs.add(ta)` happens.
2. **`use_no_ack` destination** — which TX-descriptor field/bit it sets, in which builder
   (`file:line` + the constant/bit name). Does the chip HW-retransmit when `use_no_ack=False`?
   **Flag loudly any driver that ignores the param or wires it differently.**
3. **enable_ack_detect / disable_ack_detect** — register write (which register + bit + value) or
   software-only flag? `file:line`.
4. **ACK-frame recognition in RX** — where/how the RX decode identifies an ACK control frame
   (FC byte `0xD4`) and extracts the RA; the offset to the 802.11 frame *after* the chip's HW RX
   descriptor is stripped (this offset differs per family). `file:line`. Confirm `_note_ack` is
   called there with `frame[4:10]`.
5. **_await_ack / _our_tx_macs / _note_ack / acks_seen** — identical to the baseline above, or
   different? Record any deviation (poll interval, set semantics, whether/where it's cleared).
6. **active-monitor** — `FAKE_MAC` value; enter/exit mechanism (register-MAC vs firmware BSS);
   `file:line`.
7. **Anomalies** — anything that doesn't fit the baseline. **This is the point of the exercise.**

## Divergence axes to hunt specifically

- **`use_no_ack` descriptor field** varies per chip family (Ralink TXWI vs Realtek txdesc vs
  connac2 txd vs ath9k_htc txwi) — confirm it's honored in each, and find any that drop it.
- **enable_ack_detect** register-write vs software-flag (known to split; get the exact register
  per driver, and whether a software-only card can actually see ACKs at all).
- **RX ACK recognition / RA offset** — each family strips a different HW RX descriptor before the
  MPDU, so the ACK-frame + RA offset differs; a driver that gets this subtly wrong would silently
  under-count ACKs. High-value to check per driver.
- Any driver where `_note_ack` / `_our_tx_macs` / `_await_ack` deviates from the common shape.
- `ta = frame[10:16]` is valid for the mgmt/data frames we inject (non-QoS); confirm no driver
  mishandles it.

## Method

One subagent per driver (or small batches), each returning the filled schema for its driver(s) as
structured text. The main agent compiles into: **(1)** a per-driver table, **(2)** a
**Divergences** section listing every inconsistency, each tagged with the drivers it affects — the
decision menu for the user. Verify all 22 were read (no blanks). Do **not** propose the fix.

## Deliverable

A single doc (or the compiling agent's context) with the per-driver table + the Divergences menu +
an explicit "all 22 read" confirmation. The user picks which inconsistencies to make consistent.
