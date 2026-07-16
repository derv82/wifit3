# ACK redesign — Driver parent class

Status: `chips/driver.py` implemented per your notes. Nothing else touched. Not committed.
Suite green (1737 passed), lint clean. The 22 drivers are UNCHANGED, so the new base code is
dormant (they still override the public methods and never call `super().__init__()`).

## Vocabulary (your question)

- `chips/driver.py` = the **parent** `Driver` class (an ABC).
- the 22 `chips/*/driver.py` = **subclasses** (or "Driver subclasses"). "children" also reads fine.
Used "parent" / "subclass" in the docstrings.

## What went into chips/driver.py

New base `__init__` (3 fields, replaces the old 7-field-per-driver set):

```python
self._tx_ack_enabled: bool = False              # detection armed?
self._seen_ack: bool = False                    # ACK to _tx_mac_address seen since reset
self._tx_mac_address: Optional[bytes] = None    # source MAC of our in-flight inject
```

Concrete public methods + per-chip hooks (hooks `raise NotImplementedError`, per your note):

- `inject_frame(...)` — the resend loop + wait-for-ack, calls `await self._inject_frame(frame_bytes, use_no_ack)`
- `_inject_frame(frame_bytes, use_no_ack)` — hook: one send, no retry/wait
- `_extract_mac(frame_bytes)` — `frame_bytes[10:16]` (you referenced it)
- `enable_ack_detect()` / `disable_ack_detect()` — reset state, then `await self._{en,dis}able_ack_detect()`
- `_enable_ack_detect()` / `_disable_ack_detect()` — hooks: hardware RX-filter only
- `record_ack(frame)` — `ra = frame[4:10]; if ra == self._tx_mac_address: self._seen_ack = True`
- `acks_seen(...)` — **removed** from the base.

## Names I had to reconcile (confirm)

Your notes used two names for two things. I picked one each:

- `_tx_mac_addr` (in inject) vs `_tx_mac_address` (in record_ack) → used **`_tx_mac_address`**.
- `_tx_mac_enabled` (in inject) vs `_tx_ack_enabled` (in enable/disable) → used **`_tx_ack_enabled`**.

## Details you glossed — decisions before the fan-out

1. **Seq-stamp location.** Today each driver stamps the sequence number ONCE before the resend
   loop, so every resend re-sends the identical frame (AP dedups). The new base loop calls
   `_inject_frame` once per attempt. If stamping moves inside `_inject_frame`, each resend gets a
   NEW seq. Decide: stamp once (base, before loop) or per-send (driver). Old behavior = stamp once.

2. **wait-for-ack mechanism.** I implemented a 1 ms poll on `_seen_ack` with `wait_for_ack` as the
   timeout, mirroring the old `_await_ack`. The old code used a `ts > since` compare to reject a
   stale ACK; the bool instead resets to False at the top of each attempt. Practically equal,
   slightly less precise. Confirm the bool is what you want, or keep a timestamp.

3. **SW-only drivers + the `raise` hook.** `_enable_ack_detect` raises by default, so the 12
   software-only drivers must override it with a no-op `return`. Alternative: make the base hook a
   no-op `return` so only the 10 register-write drivers override. You wrote `raise`; I kept it.

4. **`record_ack` has no `_tx_ack_enabled` guard** (matches your snippet). Harmless: `_seen_ack` is
   reset every inject/enable, so a stray ACK while disarmed can't survive to be read.

5. **`acks_seen` removal is cross-file** (not done — outside driver.py):
   - `campaign.py:462` uses it to set `_ap_ever_acked`. It is NOT a plain deletion — dropping the
     clause trips `_ap_ever_acked` on attempt 1 regardless of any ACK. It must read the new signal:
     `driver._seen_ack` (or a public `seen_ack` accessor). Pick the accessor.
   - Then strip the `acks_seen` asserts from the 22 `test_ack_detect.py` and delete each driver's
     `acks_seen` method.

6. **`enter_active_monitor` is untouched and is the OTHER side** (make the chip *emit* ACKs for a
   forged MAC), separate from `_enable_ack_detect` (admit *incoming* ACKs to RX). Left as-is.

## The 22-driver fan-out (subagent work, once the above is settled)

Per driver:
1. `__init__`: add `super().__init__()`; delete the 4 old fields (`_ack_detect_on`, `_our_tx_macs`,
   `_ack_sightings`, `_ack_last_ts`).
2. RX tap: replace the inline count with
   `if len(frame) == 10 and frame[0] == 0xD4: self.record_ack(frame); continue`.
3. Rename `inject_frame` → `_inject_frame(self, frame_bytes, use_no_ack)`; strip the resend loop,
   the `_await_ack` wait, and the `_our_tx_macs.add`. Keep only the single send (per decision #1 on
   stamp location, #C on its lock/executor).
4. Rename `enable_ack_detect`/`disable_ack_detect` → `_enable_ack_detect`/`_disable_ack_detect`;
   strip the state resets (base does them); keep only the register write. SW-only drivers: no-op.
5. Delete `_await_ack` and `acks_seen`.

## Your hardware auto-ACK question — you're not mistaken

Two real cases where the silicon handles ACKs itself:

- **Ralink + MediaTek (the A1 group), 802.11 MAC-layer retransmit.** With `use_no_ack=False` the
  chip expects the AP's ACK and retransmits in hardware if it is missing. That is the chip waiting
  on its own ACK, below our software layer.
- **A hardware TX-status FIFO on some MediaTek chips.** `wps/README.md:338` notes mt76x2u could read
  `MT_TX_STAT_FIFO` to learn per-frame ACK success directly, as a low-latency alternative to our
  monitor-RX sniff. Deferred, but it means `_seen_ack` could later be fed by hardware on those
  chips instead of `record_ack`.

Our `record_ack` path (sniff ACK frames off monitor RX) is the chip-agnostic route that works
everywhere; it is redundant with the HW capability on those specific chips, not a replacement for it.
`enter_active_monitor`'s register-MAC write (13 drivers) is the third, separate case: it makes the
chip emit ACKs for a forged MAC.

---

## ACK feature model (working definitions — NOT authoritative, verify on hardware)

Three separate features that `use_no_ack` / `ack_detect` have been conflating. Names are proposed.
Grounded in our own code where cited; every "why the silicon does X" claim is unverified.

### A — RX ACK Admission  (proposed `enable_rx_acks` / `disable_rx_acks`; today `enable_ack_detect`)
- Configures the RX/MAC filter so ACK control frames (FC=0xD4) reach our RX feed. Receive-side
  only, changes no TX behavior. `record_ack` needs it ON to see any ACK.
- Mechanism split (per ACK-STATE-FINDINGS axis B): 10 drivers flip a register (Realtek RXFLTMAP1
  b13 / MediaTek MT_RX_FILTR_CFG b10 / mt7921au FW MCU); 12 rely on the monitor filter already
  admitting ACKs (SW flag).

### B — HW ACK-Retry  (the `use_no_ack` descriptor knob)
- A TX-descriptor setting: expect the recipient's ACK, retransmit up to N times if absent.
  Fire-and-forget, no landing report reaches software.
- 8821cu: inject builds the txdesc with `retry_ctrl = not use_no_ack` → `RTS_DATA_RTY_LMT` = 6 or 0
  (`driver.py:335-336`, `tx.py:92`). Its txdesc `mac_id` defaults to `RTW_DEFAULT_MGMT_MACID = 1`
  (`tx.py:24,50,75`).
- Split (FINDINGS axis A): 9 wire an ACK bit, 2 wire a retry limit (8821cu, 8187), 11 ignore it.

### C — SW ACK-Detection  (`record_ack` + `inject_frame(wait_for_ack)`)
- Software sets `_tx_mac_address` = injected Addr2, sends once, watches RX for an ACK whose RA ==
  that Addr2, resends up to `max_resends`, returns whether it landed (`_tx_ack_seen`).
- Works under spoofing (matches the Addr2 we control). Gives a landing signal. Requires A.

### Adjacent: Active Monitor  (`enter_active_monitor`)
- 8821cu: writes the (spoofed) MAC into `REG_MACID` (0x0610), the port-0 MAC register
  (`driver.py:367` → `mac.py:291`). Makes the card HW-ACK frames sent TO that MAC.

### Bench A/B run, 2026-07-15 (a script run + my reading of it — NOT proof, NOT authoritative)

What ran: `scripts/ack_lab/ack_lab.py`. One card injects a client->AP deauth carrying a FIXED fake
Addr2 (02:11:22:33:44:55) at a target; the other card, in monitor, counts on-air copies per HW
sequence number. Exact commands (8821cu injecting, 8812au sniffing; `--inject-card 8812au` swaps the
roles and uses HW-default retry; `<real-AP>` = a BSSID passed at runtime, not stored here):

    ack_lab.py --target <real-AP>          --channel 1   --retry 15 --active-monitor 0 --count 300
    ack_lab.py --target 12:34:56:78:9a:bc  --channel 1   --retry 15 --active-monitor 0 --count 300
    ack_lab.py --target <real-AP>          --channel 149 --retry 15 --active-monitor 0 --count 150
    ack_lab.py --target 12:34:56:78:9a:bc  --channel 149 --retry 15 --active-monitor 0 --count 150

What was OBSERVED — average on-air copies per injected frame (n = 100-300 per cell):

| injector            | band | real AP | unreachable addr |
|---------------------|------|---------|------------------|
| 8821cu (retry=15)   | 2.4G | 1.77    | 14.45 |
| 8821cu (retry=15)   | 5G   | 1.23    | 15.82 |
| 8812au (HW default) | 2.4G | 1.19    | 39.39 |
| 8812au (HW default) | 5G   | 1.01    | 32.92 |

Also observed: `--retry 0` on the 8821cu gave a flat 1.00; active-monitor ON vs OFF made no measurable
difference on the same cell (1.72 vs 1.76). The 8812au retransmitted to ~48 against the unreachable
address (its HW-default limit).

What this is CONSISTENT WITH (my interpretation, not established): the card retransmits an injected
frame until the destination ACKs, capped at the retry limit, and does so for a fake source MAC with
active monitor off. Real AP -> few copies; unreachable address -> many.

Holes that keep this from being proof:
- **The sniffer never captured a single ACK (AP_acks=0 in every run).** The "AP ACKs -> HW stops" step
  is inferred from the copy count dropping, not observed. Some other difference between a present AP
  and an absent address could suppress retransmits.
- One fake MAC, one unreachable address, one frame type (deauth), one sniffer with real capture loss
  (real-AP seqs_seen < count). No randomization, no repeats across time/position.
- Two Realtek cards only.

Toward proof: capture the AP's ACK and tie each low-copy frame to a seen ACK; vary the fake MAC and the
unreachable target; a non-Realtek card; repeat runs. Until then this is suggestive, not a conclusion.

(One observation worth keeping regardless: the 8812au — labelled "A3 / use_no_ack IGNORED, no retry
field" in FINDINGS — still retransmitted ~48x to the unreachable address, so "IGNORED" is not "no
retry"; its HW retry is on by default. That label is misleading whatever we decide about B.)

---

## ACK Lab Scripts

Two bench probes in `scripts/ack_lab/`. Both drive the same `WlanDeviceManager().refresh()` +
`iface.connect()` path the app uses (`connect()` is the full cold bring-up, monitor mode included),
use only the shared driver interface so they run on any chipset, and pick cards by a case-insensitive
substring of the adapter name that must support the requested channel.

### tx_retries.py -- HW ACK-based retries
Does a card's hardware retransmit an injected frame until the *target* ACKs? The injector fires
spoofed-client deauths (Addr2 = a fake source) at a target; a second card sniffs and counts on-air
copies per HW sequence number, and counts the target's ACKs (RA = the fake source) via its ACK tap.
A target that answers collapses the copy count toward 1; one that never answers piles up to the card's
retry limit. Four scenarios: active monitor off/on, crossed with `--target` and a hardcoded bogus
(unreachable) address. On a card that can't spoof, the active-monitor pass is replaced by one that
injects as the card's own silicon MAC.

    uv run python scripts/ack_lab/tx_retries.py --inject-card 8812 --target <BSSID> --channel 1

Per scenario it prints a one-line summary (frames seen, total copies, ACKs back, median) and a
vertical histogram of copies-per-inject.

### rx_autoack.py -- HW auto-ACK
Does a card's hardware *send* an ACK for a frame addressed to it? The card under test enters active
monitor for a spoofed MAC; a prober injects unicast frames to that MAC (sourced from a fixed probe
MAC) and counts the ACKs coming back to the probe MAC via its own ACK tap. Controls: active monitor
off, and a bogus MAC. It also probes the card's own silicon MAC. No AP needed.

    uv run python scripts/ack_lab/rx_autoack.py --test-card 8822 --channel 1

Every control returning ~0 is what validates a run.

## HW ACK-Based Retries -- comparison (bench, 2026-07-16)

Does the card's hardware stop retransmitting an injected frame once the target ACKs? "real AP" is a
target that answers (lower copies = it stopped sooner). "dead target" is an address nothing answers,
so its copy count is 1 + the card's retry count and approximates the HW retry limit (sniffer capture
loss makes it a slight undercount; the 8812au's histogram peaks near 48). Median on-air copies per
inject.

| card | stops on the target's ACK? | copies, real AP | copies, dead target (approx retry limit) |
|------|----------------------------|-----------------|------------------------------------------|
| RTL8812AU | yes, spoofed source           | 2G ~1, 5G ~1 | 2G ~39, 5G ~33 |
| RTL8822BU | yes, spoofed source           | 2G 1, 5G 3-4 | 2G ~10, 5G ~13 |
| MT7612U   | yes, but only active-monitor ON | AM on: 2G 1, 5G 1 (AM off: 2G 11, 5G 16, ACKs ignored) | 2G ~13, 5G ~16 |
| MT7921AU  | yes, but only active-monitor ON | AM on: 2G 1, 5G 1 (AM off: 2G 13, 5G 15, ACKs ignored) | 2G ~13, 5G ~15 |
| RTL8187L  | own silicon MAC only          | spoofed 5-6, silicon 1 | ~6 |

The Realtek 8812/8821/8822 key the ACK match on the frame's own Addr2, so a spoofed source stops on
ACK with no active monitor needed. The 8187 keys on its own hardware MAC. The MT76 family (7612 + 7921)
keys on the source MAC only once active monitor has registered it: with active monitor OFF both
retransmit to their ~15 limit even while the AP ACKs every copy (see the high ACKs-back count in that
pass); with it ON they collapse to a median of 1. An earlier read called the 7921 "fixed count
regardless of ACKs", but that was the active-monitor-ON pass being skipped because its FAKE_MAC was
mis-flagged UNIMPLEMENTED (now SPOOFABLE, and the AM-ON pass runs).

## HW Auto-ACK -- comparison (bench, 2026-07-16, n=100)

Does the card's hardware answer a frame addressed to it with an ACK? Numbers are ACKs counted back per
100 injected frames; controls (active monitor OFF, bogus MAC) were 0-1 in every run.

| card | spoofed MAC, AM on | spoofed MAC, AM off | own silicon MAC | bogus (control) |
|------|--------------------|---------------------|-----------------|-----------------|
| RTL8812AU | yes (2G 104, 5G 100)   | 0   | yes (2G 107, 5G 100) | 0 |
| RTL8822BU | yes (2G 108, 5G 100)   | 0   | yes (2G 111, 5G 100) | 0 |
| MT7612U   | yes (2G 97, 5G 80)     | 0   | yes (2G 104, 5G 100) | 0 |
| MT7921AU  | yes (2G 102, 5G 100)   | 0   | no (2G 0)            | 0 |
| RTL8187L  | n/a (no active monitor) | n/a | yes (2G 111)        | 0 |

`FAKE_MAC` flags reconciled against this bench:
- **MT7921AU** was flagged `UNIMPLEMENTED`; it auto-ACKs a spoofed MAC on both bands (not its own
  silicon MAC), so it is now `SPOOFABLE`. [fixed]
- **MT7612U** auto-ACKs a spoofed MAC AND its own silicon MAC (the silicon-MAC ACK is where it differs
  from the 7921); already flagged `SPOOFABLE`, which stands.
- **RTL8187L** was flagged `NONE`; it auto-ACKs its own silicon MAC (not a forged one, and it has no
  active monitor to program one), so it is now `FIXED_MAC`. Re-confirmed on the bench 2026-07-16:
  injecting as the silicon MAC stops on the AP's ACK (median 1 copy), a spoofed source never does
  (median 5, its ACKs ignored). [fixed]

Caveats: one bench, these five adapters, one AP-free RX setup (the prober self-detects the ACK). Not a
substitute for reading the silicon, but the controls hold.

Note on the retry histogram: the tx_retries per-inject copy count is only valid once each inject
carries a distinct 802.11 sequence number. The MT76 chips transmit the MPDU's seq_ctrl verbatim, so a
driver that never stamps one sends every inject as seq 0 and the sniffer folds a whole run into one
bucket. mt7921au already stamped (tx.stamp_seq_ctrl); mt76x2u did not until 2026-07-16, so its numbers
here are from the post-fix build.
