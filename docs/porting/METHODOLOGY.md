# Porting a chipset

Port the chip's Linux driver to Python. Use the DKMS/vendor driver when one exists. The C source is the
spec; live hardware is the test. A capture of the Linux driver is required (Step 1): it pins the driver version to
port and shows monitor mode works.

Also read: GOTCHAS.md (bugs earlier ports hit), CHIP-DOC.md (the `<CHIP>.md` shipped with the port),
FINAL-CHECKLIST.md (before calling the port done).

## What wifit3 needs from a driver

- **Monitor mode only.** No managed mode, no association. Port the STA setup the driver runs before monitor
  entry when the bring-up goes through it.
- **20 MHz channels only.** EAPOL, management and control frames go out at 20 MHz, and APs stay backward
  compatible with 20 MHz clients. Don't implement 40/80/160 MHz tuning, but port the code inside a
  width-conditional when it runs for the 20 MHz path or sets shared state.
- **Every channel the silicon tunes:** 2.4 GHz 1-14 (channel 14 for users in Japan) and 5 GHz including DFS
  (users in Europe), whether or not a capture covers them. Build `SUPPORTED_CHANNELS` from `Driver.CHANNELS_*`
  (`chips/driver.py`); the app hops DFS with the DFS pref on.
- **`inject_frame()` is the app's only TX path.** It takes a raw 802.11 MPDU without FCS (the chip appends it) and
  sends it once; the chip's hardware retry is the only retransmission. Stamp a sequence number unless the
  hardware assigns one (`_stamp_tx_seq`).
- **Active monitor mode:** `enter_active_monitor(mac)` makes the chip hardware-ACK a forged MAC and retry until
  ACKed. Declare what the silicon can do in `FAKE_MAC` (`FakeMacSupport`: `NONE`, `FIXED_MAC`, `SPOOFABLE`;
  the default `UNIMPLEMENTED` means not ported). Ports that implement it, one per family: mt76 `mt7921au`,
  rt2x00 `rt2800usb`, Realtek DKMS `rtl8812au_dkms`, rtw89 `rtl8922au`.
- **ACKs reach RX** when asked: `_enable_rx_acks` / `_disable_rx_acks` admit ACK control frames so `record_ack`
  can count them.
- **RX** hands each frame to the `register_rx_callback` sink as a parsed `Packet`, built from the MPDU without
  its FCS.
- **Firmware** ships in `chips/<chip>/assets/`; record its source, licence and sha256 in `docs/FIRMWARE.md`.

## What a port may edit

- `src/wifit3/chips/<chip>/`: the port. Nearly every change goes here.
- `src/wifit3/device/manager.py`: only to tell this chip apart from a different chip sharing its VID:PID
  (`_VIDPID_FAMILIES`).
- `scripts/chips/<chip>/`: the port's scratch dir: extractors, `test_hw.py` (the user plugs in the card, you
  test and debug), diagnostics.
- `src/wifit3/chips/products.py`: the card's retail model, if missing.
- `docs/SUPPORTED-HARDWARE.md`: the card's entry.
- Card art, optional: offer the user to draw the card as 20x10 ANSI art with textual-paint
  (`uvx --python 3.11 --with "setuptools<82" textual-paint`), saved as `src/wifit3/ui/assets/cards/card-<model>.ans`
  and mapped in `src/wifit3/ui/screens/focus_v2/art.py`. Conventions: `src/wifit3/ui/assets/cards/README.md`.
- At FINAL-CHECKLIST.md only: `docs/CREDITS.md`, `docs/FIRMWARE.md`, the `README.md#supported-hardware` row, and
  unit tests in `tests/chips/<chip>/`.
- Nothing else: not the `Driver` ABC (`chips/driver.py`), not `wlan/`, not another chip's package.

Look at other ports for how wifit3 does a thing (active monitor, ACKs in RX, the RX reader, warm attach). Take the
chip's behaviour, values and tables only from its own C source.

## Code style in `chips/`

- Keep the C names for constants, variables and functions: a matching name is its own cross-reference.
- Add a `file:line` only when the name can't carry the link.
- Follow the C's structure. Where Python forces a difference (a split or merged helper), cite the C `file:line`.
- Never type a register address or bitfield from memory: extract them from the source with a script. Earlier
  ports' extractors are in `scripts/chips/*/extract_*.py` and `gen_*.py`: reuse their code on this chip's source,
  never their output.
- Dated history and debug notes go in the chip doc, not the `.py`.
- Each port stands alone: don't import another chip's module or extract shared code into a base, even when tables
  or sequences look identical. Each port follows its own source, and those diverge; shared code turns into
  `if chip X: ... elif chip Y:` and makes every edit a re-test of every device using it. `rtw88_base` and
  `rtl88xxau_base` exist; don't add to them or create new ones.
- A register read can change device state: never assume two reads commute; never reorder them relative to the C.

## Prerequisites

- **uv** for all Python — `uv run …`; bare `python` lacks the deps.
- **tshark** on PATH — `pcap_slicer.py`, the shared replays and many chips' `verify_pcap.py` shell out to it.
- **The card, bound to userland** — Windows: WinUSB via Zadig. Linux: the splash's device-setup
  step writes a per-chipset modprobe blacklist + udev access rule and asks for a replug. The
  blacklist (*not* the udev rule) is what keeps the kernel off the card across replugs (a rule
  only chmods the node; the kernel still binds + taints cold state). Ad-hoc: `rmmod` the module and
  run as root.
- **Never write EFUSE/EEPROM fuses**: they are one-time and permanent.

## Step 0 — Pick the driver

- **Chip already ported?** Stop. Don't write a second driver for it (no `<chip>_dkms`, no `<chip>_v2`). The
  maintainer decides whether a chip needs a new driver: raise it in an issue first.
- **Driver** — it must support monitor mode and injection; check whether it supports active monitor. The
  kernel's in-tree driver isn't always the best one: prefer a trusted DKMS fork (lwfinger, morrownr) when it
  does more or does it better. Ask the user when unsure.
- **Mode** — ask the user: *kernel-dev* (show each function as you port it, sign-off per milestone) or
  *autonomous* (port the whole driver, the user reviews at the end; the default).

## Step 1 — Capture the Linux driver

A capture is the usbmon pcap plus everything recorded with it: the step timestamps (`main.log`), the driver logs
(`driver.log`), the over-air pcap, the firmware blob and, for DKMS, the driver source. It is required: it records
the exact driver and its source, shows that monitor mode and injection work, and maps the bring-up before you
write code.

- **Capture** — on a Linux machine, card unplugged: `sudo python src/wifit3/scripts/capture.py`
  (`--target <BSSID>` adds an injection test). It records the usbmon pcap of plug-in, firmware, monitor mode, a
  channel sweep and injection, with per-step timestamps (`main.log`), the firmware blob and, for DKMS, the driver
  source, under `captures_<chip>/`. Keep it in `driver_captures/captures_<chip>/` (gitignored: real MACs).
- **Monitor mode works** — the over-air pcap must hold unicast frames between other stations.
- **Source** — the exact version the capture was taken with: DKMS captures include `driver-source/`. Mainline:
  read the driver name and vermagic from `driver.log` and fetch that tag of `torvalds/linux`.
- **Map the bring-up** — `uv run python scripts/porting/pcap_slicer.py <main.log> <pcap>` lists
  each step's frame range and frame count: where firmware, init, monitor, each channel and injection fall, and
  how much each one is.

### Replaying the capture: verify_pcap

`uv run python scripts/porting/verify_pcap.py <chip> [capture]` runs the port against the capture: it answers the
driver's reads with the card's recorded responses and compares the driver's writes with what the Linux driver
sent. The pieces: the dispatcher, a replay shared per USB family (`rtw88_pcap_replay.py` Realtek,
`rt2x00_pcap_replay.py` Ralink, `mt76usb_pcap_replay.py` mt76x0u/mt76x2u), and the chip's own
`scripts/chips/<chip>/verify_pcap.py` (`run(capture) -> int`).

The reference gate is `scripts/chips/rtl8922au/verify_pcap.py`. A gate:

- drives the driver's own `_bringup()` (`connect()` minus the USB interface claim), `set_channel()` and
  `inject_frame()`; not `_tune()` or `_inject()`, and not the chip's modules one by one;
- reads only runtime inputs from the capture, like the channel a hop targets (`_peek_channel`); never pre-parses
  EFUSE or other state the driver reads itself;
- absorbs async traffic by its signature (`_absorb_async`), not by skipping a fixed number of ops;
- names every waiver with its reason.

Use it to check a ported step before it touches hardware, and to debug bring-up. A divergence marks code to
compare with the C, not a byte to copy.

- Don't read the capture op by op and reproduce bytes you don't understand: that corrupts chip state.
- Don't change driver code that works on live hardware because of a divergence; inspect that area against the C.
- Don't chase every divergence: watchdog ticks and LED blinks land anywhere. Find the kernel's periodic work
  (`INIT_DELAYED_WORK`, watchdog, link tuner, DIG), then its signature in the capture, and handle it in the chip's
  verify_pcap (call the driver's tick directly, or drop it).
- Replay every capture of the chipset, not one: it separates a watchdog tick from a `set_channel` write, and
  shows bugs one capture hides.
- A full match doesn't mean the port is done: timing, races, missing waits for device ACKs and missed sleeps
  don't show in a replay.
- TX: pass the 802.11 frame from each captured bulk-OUT URB to `inject_frame()` and byte-diff the URB the driver
  sends against the captured one. If the capture has no bulk-OUT URBs, record that and test TX on air.
- RX: feed the captured bulk-IN buffers through the driver's RX path (`iter_frames` + `WlanFrameParser`) and
  check that frames and beacons decode (rtl8922au's `validate_rx`).
- Channels: start with the capture's `iw set channel` sweep (one tune at a time, long gaps). The airodump-ng
  segment hops every 0.25 s, in a fixed but non-sequential order across every band, with async traffic
  interleaved: leave it for last.

## Step 2 — Port the chipset's paths

The chip package (`src/wifit3/chips/<name>/`):

- **`__init__.py` declares the VID:PIDs**: `SUPPORTED_IDS: list[DeviceID]` (every VID:PID this driver claims, a
  readable description, any chip-id discriminator in `extras={}`) and
  `def import_driver(): from .driver import <Class>; return <Class>`. It must NOT import `driver.py` at module
  top: discovery is a pkgutil walk over `chips/*` that reads each light `__init__` and imports the driver only
  on a VID:PID match. Copy the shape from `chips/rtl8812au/__init__.py`.
- **`driver.py` subclasses the `Driver` ABC** (`chips/driver.py`): class attr `SUPPORTED_CHANNELS`
  (`SUPPORTED_IDS` is not on the class), classmethod `from_usb_device(cls, dev, id_entry)`, and `connect()`,
  `set_channel()`, `inject_frame()`, `close()`, `register_rx_callback()`.
- **Cold vs warm:** if the vendor probe re-initialises a running chip, `connect()` always runs the cold path.
  Otherwise it may detect a chip left running and skip bring-up (`chips/rtl8821au/mac.py:is_chip_warm()`,
  `driver.py:_warm_reattach`).
- **Registration:** nothing to register unless a different chip shares this chip's VID:PID: then add a
  `_VIDPID_FAMILIES` row with a resolver in `device/manager.py`.

- Port every line the C runs for this chipset: every branch, helper and switch case, both init and start. In a
  driver for many chipsets (rtl8xxxu, rt2800usb), port only the paths this chipset's IDs and chip checks reach.
- Bring the card up in the C's order. Don't reorder or defer steps to demo something early: cards calibrate RX/RF
  at specific points.
- Port every EFUSE/EEPROM path the C has: a card with different values must still work. Never hardcode this
  card's values (BUGS.md "Expand EFUSE support" lists the ports that did).
- Start the chip's reference doc (CHIP-DOC.md) as you go.

Don't judge a branch irrelevant by its name. "Looks like Bluetooth," "the 40 MHz path,"
"coexistence": these are exactly where drivers hide RX/RF-critical writes. Read what the code
*writes*, not what the function is called. A coex init block has held RX tuning we needed; a
width-conditional has held shared tuning the 20 MHz path depended on.

A branch this card's silicon can't trigger is still ported, behind its real runtime check (the
`if (efuse->X)` / cut / cap test from the source), marked `# TODO: verify, untested here, needs
<hardware>` with its citation. That's "ported, untested," not "skipped," and it's what lets a
sibling card work.

## Step 3 — Test on hardware

- Before live testing, ask the user for test APs, at least one per band the card supports. Live TX (injection,
  deauth, attacks) goes only at those APs.
- Write `scripts/chips/<chip>/test_hw.py` and run it yourself: `scripts/dev.py` picks the card (name substring or
  bus:addr), so the script can bring it up and run RX or TX on it.
- Compare RX with the Linux driver: `scripts/baseline/BASELINING.md` (APs seen, beacons/s of nearby reference
  APs). The RF environment drifts: alternate runs A/B/A/B instead of comparing against an old number.
- Try to break it: switch between Scanner and Focus quickly, hop hard, replug mid-run (ask the user). A card that
  goes deaf points at `set_channel`, RF tables or band switching.
- If the device wedges, ask the user to unplug and replug: that resets cold-boot state.
- Then run FINAL-CHECKLIST.md.
