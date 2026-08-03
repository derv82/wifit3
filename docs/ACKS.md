# ACKs: diagnosing hardware auto-ACK and retry

## Wifit3's ACK architecture

Three layers. A **campaign** (pmkid / WPS-pin / WPS-pbc / wep) asks a **`WlanInterface`** to inject
and to watch for the recipient's ACK; the interface delegates to its **`Driver`**, which owns every
ACK mechanism; each chip's RX reader taps `record_ack` on incoming ACK frames. Two independent things
travel under the word "ACK": whether we *hear* the recipient's ACK (the RX tally) and whether our card
*emits* an ACK for a chosen/spoofed MAC (active monitor + the chip's own TX HW-retry). The four
measured features are named in the glossary below (A/B/C + Active Monitor); this is where each lives.

Typical ACKed exchange (WPS / PMKID): `set_fake_mac()` (so the AP's unicast reaches us and the chip
auto-ACKs it, or the AP abandons the session) → `enable_rx_acks()` (arm the tally) → `send_until_ack()`
/ `acks_seen()` (did the AP ACK our frame?). Deauth needs none of this: it only *reports* endpoint ACKs.

### `Driver` (ABC, `chips/driver.py`): owns the mechanism

- **Injection**
  - `inject_frame(frame)`: send once, fire-and-forget; registers the frame's Addr2 in the tally,
    stamps the seq, calls the chip hook. The chip's own HW ACK-retry is the only retransmission.
  - `inject_frame_slow_retry(frame, timeout, max_resends)`: software ACK-retry: send, watch RX for an
    ACK to our Addr2, resend on silence up to `max_resends`. Needs the tally armed (feature C).
  - `_inject_frame(frame)` *(hook)*: build the chip's TX descriptor and bulk-OUT it once.
  - `_stamp_tx_seq(frame)` *(hook)*: stamp an incrementing 802.11 sequence, or identity if the HW
    assigns it (the per-inject seq the sniffer keys the retry histogram on).
  - `_extract_mac(frame)`: the Addr2/TA (`frame[10:16]`) the recipient's ACK returns to.
- **RX-ACK tally: do we *hear* the ACK? (features A + C)**
  - `enable_rx_acks()` / `disable_rx_acks()`: arm/disarm the tally: reset state, then call the hook.
  - `_enable_rx_acks()` / `_disable_rx_acks()` *(hooks)*: flip the RX-filter bit that admits ACK
    control frames (FC=0xD4) to RX (Realtek RXFLTMAP1 / MediaTek MT_RX_FILTR_CFG), or a documented
    no-op on cards whose monitor filter already admits them.
  - `record_ack(frame)`: a chip's RX reader calls this per ACK; tallies it when armed and the ACK's
    RA (`frame[4:10]`) is a source MAC we injected as.
  - `acks_seen(mac)`: ACKs tallied to source `mac` since the last arm.
  - state (base `__init__`): `_ack_detect_on` (armed?), `_our_tx_macs` (MACs we count ACKs for),
    `_ack_counts` (MAC → count).
- **Active monitor: does our card *emit* an ACK? (feature D)**
  - `enter_active_monitor(mac, bssid)`: program `mac` into the chip's MAC register so the HW
    auto-ACKs frames addressed to it while staying in monitor mode. Base raises `NotImplementedError`;
    SPOOFABLE / FIXED_MAC chips override.
  - `exit_active_monitor()`: restore the card's real MAC (stop ACKing the forged one).
- **Capability / timing (class attrs)**
  - `FAKE_MAC` (a `FakeMacSupport`): the chip's auto-ACK ability, ordered `SPOOFABLE` > `FIXED_MAC` >
    `NONE` > `UNIMPLEMENTED`. Drives `enter_active_monitor` support and TX-card election.
  - `MAX_ACK_DELAY`: the slow-retry wait window (~20 ms RX-tap round-trip vs ~10 us on-air).

### `chips/<chip>/driver.py` (+ `rx.py`): per-silicon hooks

- `_inject_frame` / `_stamp_tx_seq` / `_enable_rx_acks` / `_disable_rx_acks`: the hooks above, one
  register/descriptor path per silicon family.
- `enter_active_monitor` / `exit_active_monitor`: the MAC-register write (Realtek RCR/MACID,
  Atheros `AR_STA_ID`, MediaTek MCU cmd), on SPOOFABLE / FIXED_MAC chips only.
- RX reader → `self.record_ack(...)`: every chip's bulk-IN loop recognizes an ACK frame (FC=0xD4)
  and feeds it (full MPDU, or a synthesized `\x00\x00\x00\x00 + RA` on chips that report only the RA)
  to the base tally.

### `WlanInterface` (`wlan/interface.py`): the per-card facade campaigns call

- `send_no_wait(frame)`: fire-and-forget inject (→ `driver.inject_frame`); also fires TX stats.
- `send_until_ack(frame, max_retries)`: inject + wait for the link-ACK (→ `inject_frame_slow_retry`).
- `enable_rx_acks()` / `disable_rx_acks()` / `acks_seen(mac)`: arm / disarm / read the tally (→ driver).
- `set_fake_mac(mac=None, bssid=None)`: enter active monitor and return the MAC we'll ACK as: a random
  locally-administered MAC for SPOOFABLE, the card's own for FIXED_MAC, `None` if the card can't.
- `clear_fake_mac()`: exit active monitor.
- `active_monitor_warning()`: Rich-markup warning when the card can't HW-ACK a spoofed MAC, else `None`.
- `deauth_broadcast` / `deauth_client`: build + spray deauths; `deauth_client` tallies how many frames
  each endpoint ACKed (reads the RX tally).

### `WlanArray` (`wlan/array.py`): elects the card that TXes

- `select_iface(channel)`: the card to TX (and thus ACK) on: the user's pinned card, else the most
  auto-ACK-capable card that reaches the channel.
- `fake_mac_rank(iface)` *(module fn)*: orders cards by `FAKE_MAC` (SPOOFABLE < FIXED_MAC < NONE <
  UNIMPLEMENTED); the key `select_iface` and the TX picker rank on.
- `register_self_mac` / `register_forged_mac`: tell the shared RX sink a MAC is ours (a spoofed
  active-monitor MAC, or an injected source), so our own TX isn't ingested as a real station.

### Callers (`campaigns/`)

- `pmkid.py`, `pin.py` (WPS PIN), `pbc.py` (WPS PBC): `set_fake_mac` → `enable_rx_acks` → `send_until_ack`
  / `acks_seen`. The AP must ACK us or it abandons the EAPOL/WPS exchange.
- `wep/campaign.py`: `set_fake_mac` so ARP-replay / ChopChop frames are ACK-delivered.

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

Two bench probes in `scripts/diag/ack/`. Both drive the same `WlanDeviceManager().refresh()` +
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

    uv run python scripts/diag/ack/tx_retries.py --inject-card 8812 --target <BSSID> --channel 1

Per scenario it prints a one-line summary (frames seen, total copies, ACKs back, median) and a
vertical histogram of copies-per-inject.

### rx_autoack.py -- HW auto-ACK
Does a card's hardware *send* an ACK for a frame addressed to it? The card under test enters active
monitor for a spoofed MAC; a prober injects unicast frames to that MAC (sourced from a fixed probe
MAC) and counts the ACKs coming back to the probe MAC via its own ACK tap. Controls: active monitor
off, and a bogus MAC. It also probes the card's own silicon MAC. No AP needed.

    uv run python scripts/diag/ack/rx_autoack.py --test-card 8822 --channel 1

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
