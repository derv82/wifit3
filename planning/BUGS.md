# Wifit3 — Known Bugs & QoL

---

## Tag + suppress EAP/Enterprise handshakes — High priority (correctness, small)

An EAP (enterprise) 4-way is captured and currently emitted as "crackable,"
but its PMK comes from the EAP/MSK exchange, not a passphrase.
hashcat `-m 22000` can't touch it. Extend the "crackability gate" (handshake.py) so
an EAP-negotiated 4-way is withheld + badged "EAP/Enterprise" rather than reported as
a capture.

---

## Proposed Features

### Hardware-failure UX — pre-alpha (release blocker)

Hardware failure without any error message is a BUG.

**Problem.** When a card fails, the UI is unhelpful: an init failure shows a
generic message, a runtime wedge just lets the Scanner fade to empty, and the only
real detail lives in `wifit3.log` — which a user gets *only* by knowing to set
`WIFIT3_LOG=1` before launch. That's a developer affordance, not a user one (and
it gets worse under PyInstaller/launchers, where there's no obvious shell to set an
env var in). A user who hits a failure should get a clear, actionable message
**and** be able to see the gory details — without a terminal, without an env var.

**Headline requirement: logs/details reachable from *inside* the UI.**
`wifit3.log` (behind `WIFIT3_LOG`) stays exactly what it is — a developer trace of
the code path, intentionally hidden from users. Separately, the UI must surface
what a *user* needs when something breaks: a plain reason up front, the technical
detail one expand away. The user never learns an env var exists.

**The error modal (the shape we want).**
- Main line, red, plain + actionable: *"Driver is borked — please unplug and
  replug the adapter."*
- A collapsed **Details** disclosure holding the full technical dump: the exception
  + stack trace, plus whatever state the driver knew (e.g. "RF went dead",
  register/hex values, addresses). Copy-able. *This* is the in-UI "logs" the
  requirement above asks for.
- Dismiss returns to the splash, where device re-discovery already runs on its 1 s
  poll — so a replug recovers without relaunching.

**Mechanism — two cases, and we strongly prefer `raise()` over callbacks.**
The ideal: a driver `raise`s at the point of failure, from anywhere in its code,
and that plops the user out to the modal with the message + stack trace. Callbacks
for this are explicitly *disliked* — they scatter the failure path. How achievable
that is splits by case:

1. **Init failure — the easy half, low-invasive.** `connect()` runs inside an
   awaited Textual worker, so a raised exception already has a call stack to ride
   up to one UI-level `except`. The reason it doesn't work today is *self-inflicted*:
   every driver wraps bring-up in `except Exception: return False` and swallows the
   cause, and `connect()` piles broad catches on top. The fix is mostly **deleting**
   those swallows so the exception propagates — less code, no new subsystem. One
   catch in the splash worker → modal → splash.

2. **Runtime wedge — the hard half, no plan yet.** A driver detects mid-session
   that it's borked and needs a replug. Several drivers *can* already self-detect
   this (warm-reattach bulk-IN smoke tests, RX-dead watchdogs). The trouble: it's
   often detected on a **detached background thread** (the RX reader) or a
   fire-and-forget hop task — there's no `await` for a `raise` to bubble to, so a
   raise there just dies on that thread. Whether raising works "depends on the
   stack trace": clean when the wedge is noticed during an awaited call (e.g.
   `set_channel`), useless when it's noticed off-thread.
   - **Open question — how does an off-thread wedge become a UI `raise` without
     callbacks?** One candidate to explore (undecided): the driver stashes the
     failure as state and a UI-side poll (the Scanner already ticks) notices it and
     raises at a UI-reachable point — turning it back into the clean "raise →
     modal" flow, no callback wiring. This is the core thing to design *before* any
     code.

**Out of scope here (separate, later).** Non-fatal **toast** notifications — low
beacon rate, weak RX, per-driver `known_issues` surfaced on bring-up. Useful, but a
different mechanism and a lower urgency than "the card died, tell the user."

**Complexity.** Init half: low (delete the swallowing + one catch). Wedge half:
genuinely hard, design-doc-first — *not* a zero-shot. Confirmed failure modes to
cover when built: warm-reattach init wedge (RTL8822BU — replug message currently
lost behind a generic error) and runtime RX wedge (RTL8812AU — Scanner fades
silently).

> **Related consideration — a `BaseDriver` class (its own design, not a v1
> dependency).** Worth *considering*: an abstract `BaseDriver` that all drivers
> inherit, holding the logic genuinely common to every driver — and, since not all
> drivers run an RX reader thread (ar9271 doesn't), perhaps a
> `ReaderThreadDriver(BaseDriver)` tier for the ones that do. This is
> **significant** work: it touches all ~13 drivers and warrants a design of its own
> — the families differ enough (HTC/WMI vs direct-register vs MCU-firmware) that a
> premature base would be the wrong one.
> It's flagged *here* because it would pay off for the
> hard half above: if `BaseDriver` / `ReaderThreadDriver` already existed, surfacing
> a wedge from inside `RxReaderThread` (the off-thread `raise` problem) could live
> in one shared place instead of being re-implemented per driver — the DRY win. So:
> not required for v1, but a real reason `BaseDriver` is worth designing.

---

## rt2800usb EFUSE reader reads wrong addresses → bad freq/chain/LNA/RSSI on 3 cards

TODO: was this fixed?

**High impact, affects RT5372 / RT5572 / RT3572 today.** `chips/rt2800usb/eeprom.py`
`read_eeprom_efuse` walks the EFUSE with a **byte** offset (`range(0, 512, 16)`, writing
`EFUSE_CTRL_ADDRESS_IN = byte_offset`), but the chip wants a **u16-word** offset — kernel
`rt2800lib.c:10955` does `for i ... i += 8` (word units) and the field is
`EFUSE_CTRL_ADDRESS_IN = FIELD32(0x03fe0000)`. Block 0 (the MAC, byte 0–15) reads correctly,
which **masked the bug**; every block past byte 16 is fetched from *double* the address. So
`NIC_CONF0` (TX/RX **chain counts** + RF_TYPE), `freq_offset` (crystal trim — the documented
RX gate), `LNA` gain, `RSSI` offsets, and the RT5592 IQ-cal bytes are all read from the wrong
place. Likely consequences: off-frequency synth / weak-or-flaky RX, wrong chain/PA config,
miscompensated RSSI — i.e. real scan/RX performance loss, not a cosmetic divergence.

Why it shipped: the old `scripts/rt2800usb/verify_pcap.py` only replayed the firmware-upload
block, never the EFUSE walk — green-but-unfaithful (the "Green ≠ faithful" trap in
`planning/PORTING.md`). Found 2026-06-09 while staging the RT3070 clean-room port; the new
single-cursor full-walk gate catches it on the **2nd** EFUSE block.

Fix: `ADDRESS_IN = byte_offset // 2` (or loop in word units). **It's its own task** — the fix
shifts every EFUSE-derived value family-wide, so it needs a full-walk `verify_pcap` for the
EFUSE loop **plus an RX A/B re-verify on all three physical cards** (RT5372/RT5572/RT3572)
before/after. Deliberately left unpatched until then. The RT3070 clean-room port
(`chips/rt3070/`) ships its own correct (word-offset) reader and is unaffected. Cross-refs:
`chips/rt2800usb/RT2800USB.md` § Potential Known Gaps; `chips/rt3070/RT3070.md`. Greppable:
`EFUSE_CTRL_ADDRESS_IN`, `read_eeprom_efuse`.

## WPS PBC auto-invade can monopolize the radio on timeout (Focus)

PBC auto-invade is ON by default and works well, but in Focus a PBC attempt that
times out keeps retrying for the rest of the AP's PBC window, and other attacks
are blocked for that span. Give it manual control — a **Stop PBC** button (and a
**Start PBC** when a window is open) — and/or bound the retry loop so a single
timeout can't hold the radio. Minor; deferred.

## 5 GHz drivers under-list DFS channels the cards support (deferred — DFS ≈ empty air)

Every 5 GHz driver **except** `rtl8814au_dkms` advertises the byte-identical 9
non-DFS channels (`36,40,44,48,149,153,157,161,165`, DFS=0) — RTL8812AU / 8821AU /
8822BU / mainline-8814 / MT76x0U / MT76x2U / RT2800USB. That identical list across 7
unrelated chipsets is a copy-paste porting decision, **not** derived per-card: their
capture `iw.log`s show `iw set channel 52/100/144` returning **0** (mt76x2u, mt7921u,
rt5572 confirmed), i.e. the cards + regdomain *do* tune DFS. So those drivers refuse
channels the hardware supports. (`rtl8814au_dkms` lists all 25 incl. DFS 52–144,
byte-verified + live-hopped; it just excludes them from the *default* hop — see below.)

**Deliberately deferred, not urgent.** DFS (UNII-2, 52–144) is radar-shared so most APs
avoid it → usually empty; omitting it means faster hop cycles and few-to-no missed APs.
This is also why only `rtl8814au_dkms` hit the `bulk_in` Windows-timeout bug above — it
is the only driver that hops the empty DFS channels that produce long timeout runs.

**To add DFS later (per driver — NOT a blind list edit):** the porters who truncated the
list likely never exercised the DFS *tune paths*, so a driver with non-DFS-sized sub-band
tables would mis-tune (garbage / crash) if you just appended the channels. Do the 8814
treatment: (1) confirm `iw` accepted it in the capture (`return 0`), (2) byte-verify the
driver's `set_channel` reproduces the capture's DFS tunes, (3) then extend
`SUPPORTED_CHANNELS`. The DFS infra is already in place and stays: `wlan/channels.is_dfs`
(52–144), the scanner's non-DFS default hop, and the Channel-Filter `[d]fs` opt-in.

## Driver bring-up divergences from the vendor wire (pcap replay-diff findings)

TODO: was this fixed?

Surfaced by the per-driver pcap byte-diff verifier (`scripts/verify_pcap.py <chip>`, which
replays a port's bring-up against the vendor cold-boot capture). Each is a faithfulness gap
— the port emits a USB sequence the in-tree driver does not — found while bringing the
Ralink + legacy-Realtek families onto the verifier. None is known to break RX/TX, but each
is an un-audited deviation worth closing.

* **rt2800usb: `load_firmware` writes `AUTOWAKEUP_CFG` on USB; the kernel doesn't.** Our
  `chips/rt2800usb/firmware.py` `load_firmware` opens with `write32(AUTOWAKEUP_CFG, 0)`, but
  the RT5592 USB cold-boot capture never issues it. In `rt2800lib.c` that write sits inside
  an `is_pci || is_soc` guard — PCI/SoC only — so the USB path should skip it. The 4096-byte
  rt2870.bin upload itself is byte-perfect (verified); only this preamble write diverges.
  Fix: gate the `AUTOWAKEUP_CFG` write out of the USB loader. Greppable: `AUTOWAKEUP_CFG`.
  **Counter-claim (2026-06-10, kernel re-read):** the "PCI/SoC-only guard" half is falsified —
  `rt2800lib.c:731` writes `AUTOWAKEUP_CFG` **unconditionally**, *above* the `is_pci` block
  (which guards only `AUX_CTRL`/`PWR_PIN_CFG`, :739-750), so our write looks kernel-faithful and
  the proposed gate would be a *regression*. The wire-omission half is still unreconciled —
  but it's unverified too: this driver's verifier is anchored-block and never walks the
  `load_firmware` preamble, so nothing has actually checked whether the op is on this wire.
  **Do not apply the gate**; a single-cursor full-walk (rt3070-style) is what adjudicates it.

---
