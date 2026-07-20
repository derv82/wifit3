# ACK redesign — Driver parent class

Status: `chips/driver.py` implemented per your notes. Nothing else touched. Not committed.
Suite green (1737 passed), lint clean. The 22 drivers are UNCHANGED, so the new base code is
dormant (they still override the public methods and never call `super().__init__()`).

## What goes into chips/driver.py

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

---

## ACK feature model (proposed glossary)

Four features that `use_no_ack` / `ack_detect` had been conflating. Names are proposals for the
redesign; the hardware behaviour is measured in the two tables below.

- **A. RX ACK Admission** (`enable_rx_acks` / `disable_rx_acks`; today `enable_ack_detect`): the RX/MAC
  filter setting that lets ACK frames (FC=0xD4) reach our RX feed. Receive-side only; `record_ack` needs
  it on. Register-driven cards flip a bit (Realtek RXFLTMAP1 / MediaTek MT_RX_FILTR_CFG); monitor-mode
  cards already admit ACKs.
- **B. HW ACK-Retry** (the `use_no_ack` descriptor knob): a TX-descriptor setting to expect the
  recipient's ACK and retransmit up to N times if absent. Fire-and-forget, no landing report to
  software. Measured in "HW ACK-Based Retries" below.
- **C. SW ACK-Detection** (`record_ack` + `inject_frame(wait_for_ack)`): software sets `_tx_mac_address`
  to the injected Addr2, sends, watches RX for an ACK to that Addr2, resends up to `max_resends`, and
  returns whether it landed. Works under spoofing. Requires A.
- **Active Monitor** (`enter_active_monitor`): writes a (spoofed) MAC into the card's MAC register so the
  hardware auto-ACKs frames sent to it. Measured in "HW Auto-ACK" below.

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

Does the card's hardware stop retransmitting an injected frame once the target ACKs? Median on-air
copies per inject: "real AP" answers (lower = it stopped sooner); "dead target" never answers, so its
count is ~1 + the retry limit (AM state does not change it; sniffer loss makes it a slight undercount).
A row with only a 2G figure is a 2.4 GHz-only radio.

| card | stops on the target's ACK? | real AP (AM off) | real AP (AM on) | dead target (retry limit) |
|------|----------------------------|------------------|-----------------|---------------------------|
| RTL8812AU  | yes, keyed on Addr2 | 2G 1, 5G 3 | 2G 1, 5G 1 | 2G ~41, 5G ~49 |
| RTL8822BU  | yes, keyed on Addr2 | 2G 1, 5G 2 | 2G 1, 5G 1 | 2G ~11, 5G ~13 |
| RTL8821AU  | yes, keyed on Addr2 | 2G 1, 5G 1 | 2G 1, 5G 1 | 2G ~42, 5G ~49 |
| RTL8821CU  | yes, keyed on Addr2 | 2G 1, 5G 1 | 2G 1, 5G 1 | 2G ~7, 5G ~7 |
| RTL8188EUS | yes, keyed on Addr2 | 2G 1 | 2G 1 | 2G ~13 |
| RTL8814AU  | yes, keyed on Addr2 | 2G 1, 5G 1 | 2G 1, 5G 1 | 2G ~13, 5G ~13 |
| AR9271     | yes, keyed on Addr2 | 2G 1 | 2G 1 | 2G ~8 |
| MT7612U    | yes, needs active monitor | 2G 11, 5G 16 | 2G 1, 5G 1 | 2G ~13, 5G ~16 |
| MT7610U    | yes, needs active monitor | 2G 12, 5G 16 | 2G 1, 5G 1 | 2G ~16, 5G ~16 |
| MT7921AU   | yes, needs active monitor | 2G 13, 5G 15 | 2G 1, 5G 1 | 2G ~13, 5G ~15 |
| RT3070     | yes, needs active monitor | 2G 7 | 2G 1 | 2G ~8 |
| RT5370     | yes, needs active monitor | 2G 7 | 2G ~2 | 2G ~8 |
| RT5372     | yes, needs active monitor | 2G 7 | 2G 1 | 2G ~8 |
| RT5572     | yes, needs active monitor | 2G 7 | 2G 1 (5G: no ACK seen) | 2G ~8, 5G ~8 |
| RTL8187L   | only its own silicon MAC | silicon 1, spoofed 5-6 | N/A (no AM) | ~6 |
| RT2500USB | yes, keyed on self-MAC [1] | 2G 1/~15 (ack=off/on) | 2G 1 | 2G 1/~15 (ack=off/on) |

RT3572 (rt2800usb) is omitted from this table: its unburned-EFUSE TX is too weak to elicit AP ACKs, so
its retry behaviour is unmeasured. Its auto-ACK still confirmed it SPOOFABLE (see below).

[1] RT2500USB corrected 2026-07-19. The original "no, ignores ACKs / 2G ~14 / ~16" was wrong the
self-MAC register (MAC_CSR2/3/4) never programmed.

The stop-on-ACK match splits by chip vendor. Realtek (8812 / 8821 / 8821cu / 8822 / 8814 / 8188eus) and
Atheros (AR9271) key on the frame's own Addr2, so they stop without active monitor: both columns are ~1
on 2.4 GHz, and on 5 GHz AM-off runs at most a copy or two above AM-on (8812au 3, 8822bu 2). The MediaTek
and Ralink family (mt76x0u / mt76x2u / mt7921au, rt3070 / rt5370 / rt5372 / rt5572) keys on the source
MAC only once active monitor registers it: AM off ignores the ACK and retries to the limit, AM on
collapses to 1. The 8187 keys on its own silicon MAC only. The RT2500USB ships ack=False (one copy, no
retries); the silicon could stop-on-ACK if the self-MAC were programmed, but no shipped code does that
(note [1]). (The 7921's old "fixed count regardless" label was just its AM-on pass being skipped when
FAKE_MAC was mis-flagged UNIMPLEMENTED.)

## HW Auto-ACK -- comparison (bench, 2026-07-16, n=100)

Does the card's hardware answer a frame addressed to it with an ACK? Numbers are ACKs counted back per
100 injected frames; controls (active monitor OFF, bogus MAC) were 0-1 in every run.

| card | spoofed MAC, AM on | spoofed MAC, AM off | own silicon MAC | bogus (control) |
|------|--------------------|---------------------|-----------------|-----------------|
| RTL8812AU  | yes (2G 104, 5G 100)   | 0   | yes (2G 107, 5G 100) | 0 |
| RTL8822BU  | yes (2G 108, 5G 100)   | 0   | yes (2G 111, 5G 100) | 0 |
| RTL8821AU  | yes (2G 100, 5G 100)   | 0   | yes (2G 101, 5G 100) | 0 |
| RTL8821CU  | yes (2G 101, 5G 100)   | 0   | n/r (warm boot)      | 0 |
| RTL8188EUS | yes (2G 100)           | 0   | yes (2G 100)         | 0 |
| RTL8814AU  | yes (2G 100, 5G 100)   | 0   | yes (2G 100, 5G 100) | 0 |
| AR9271     | yes (2G 100)           | 0   | yes (2G 100)         | 0 |
| MT7612U    | yes (2G 97, 5G 80)     | 0   | yes (2G 104, 5G 100) | 0 |
| MT7610U    | yes (2G 100, 5G 100)   | 0   | yes (2G 100, 5G 100) | 0 |
| MT7921AU   | yes (2G 102, 5G 100)   | 0   | no (2G 0)            | 0 |
| RT3070     | yes (2G 100)           | 0   | yes (2G 101)         | 0 |
| RT5370     | yes (2G 102)           | 0   | yes (2G 100)         | 0 |
| RT5372     | yes (2G 100)           | 0   | yes (2G 101)         | 0 |
| RT5572     | yes (2G 102, 5G 100)   | 0   | yes (2G 101, 5G 100) | 0 |
| RT3572     | yes (2G 103)           | 0   | yes (2G 102)         | 0 |
| RTL8187L   | n/a (no active monitor) | n/a | yes (2G 111)        | 0 |
| RT2500USB  | n/a (no active monitor) | n/a | no (2G 0)           | 0 |

`FAKE_MAC` flags reconciled against this bench:
- **MT7921AU** was flagged `UNIMPLEMENTED`; it auto-ACKs a spoofed MAC on both bands (not its own
  silicon MAC), so it is now `SPOOFABLE`. [fixed]
- **MT7612U** auto-ACKs a spoofed MAC AND its own silicon MAC (the silicon-MAC ACK is where it differs
  from the 7921); already flagged `SPOOFABLE`, which stands.
- **RTL8187L** was flagged `NONE`; it auto-ACKs its own silicon MAC (not a forged one, and it has no
  active monitor to program one), so it is now `FIXED_MAC`. Re-confirmed on the bench 2026-07-16:
  injecting as the silicon MAC stops on the AP's ACK (median 1 copy), a spoofed source never does
  (median 5, its ACKs ignored). [fixed]
- **AR9271, MT7610U, RT3070, RT5370, RT5372, RT5572, RTL8188EUS / RTL8814AU / RTL8821AU / RTL8821CU
  (DKMS)** were already `SPOOFABLE` and the bench agrees (spoofed auto-ACK via active monitor, plus
  silicon-MAC auto-ACK where it could be read); no flag change. Their TX stop-on-ACK family is in the
  retry table above (the Realtek and Atheros cards key on Addr2; the MediaTek mt76x0u / mt76x2u and the
  Ralink rt30xx / rt53xx / rt5572 need active monitor). Two notes: the missing-seq-stamp bug (every
  inject left seq 0, folding the retry histogram into one bucket) hit mt76x2u, mt76x0u, rt5572 and
  rt2800usb, each now fixed to stamp an incrementing seqctl; the 8821cu came up warm so its silicon-MAC
  auto-ACK was not read (spoofed auto-ACK confirmed on both bands).
- **RT3572 (rt2800usb driver)** auto-ACKs a spoofed MAC and its own silicon MAC (103 / 102 per 100), so
  `SPOOFABLE` is confirmed. The one test unit has an unburned EFUSE, so its injected TX is too weak to
  reach the AP (0 ACKs back on every scenario): its retry family is unmeasured, not inferred. Its inject
  carried the same missing-seq-stamp bug, now fixed.
- **RT2500USB** stays `NONE` on the auto-ACK (emit) axis, confirmed and root-caused 2026-07-19: it does
  not auto-ACK even its own silicon MAC (0/100), and re-testing with MAC_CSR2/3/4 programmed still gave
  0. Root cause: the RT2570 has no `AUTO_RSP_CFG` (autoresponder enable) and no `UNICAST_TO_ME_MASK`.
  Its ACK emission is coupled to the RX address filter (TXRX_CSR2 `DROP_NOT_TO_ME`), which monitor mode
  must clear to receive foreign frames, so the responder never fires.

Scope: the mainline (non-DKMS) Realtek variants (rtl8188eus, rtl8812au, rtl8821au, rtl8822bu) and
rtw88_8814au stay `UNIMPLEMENTED` by choice: active monitor was never ported for them, so they are out
of scope for this sweep, not regressions. The bench targets the DKMS drivers we ship.

Note on the retry histogram: the tx_retries per-inject copy count is only valid once each inject
carries a distinct 802.11 sequence number. The MT76 chips transmit the MPDU's seq_ctrl verbatim, so a
driver that never stamps one sends every inject as seq 0 and the sniffer folds a whole run into one
bucket. mt7921au already stamped (tx.stamp_seq_ctrl); mt76x2u did not until 2026-07-16, so its numbers
here are from the post-fix build.
