# RTL8821CU (8821cu_dkms)

A self-contained port of the Realtek vendor/DKMS driver (`rtl8821cu-5.12.0.4`, in the capture
bundle). RTL8821C silicon: 1T1R, 2.4 + 5 GHz 802.11ac, HALMAC + PHYDM, Jaguar-2 phystatus,
firmware-based. Its power tables and init are specific enough that it doesn't share a base with
other drivers.

## Status

Cold init, firmware boot, and monitor RX all work on hardware — both bands, fixed-channel or
hopping, on par with the vendor driver (fixed-ch1 ~6.5 beacons/s vs the kernel's ~6.1–6.6/s).
Active monitor (HW-ACK a forged MAC) works too — WPS-PBC HW-confirmed (see log).

Three hardware bugs, all invisible to the byte-gate, were found and fixed (see Gotchas + log):

- the cross-band RX coin toss — `_dc_cancellation` is no longer called (its ck320 toggle killed RX).
- fixed-channel 2.4 GHz dead — a reader-quieted band bounce in `connect` (`_prime_2g_band`).
- runtime focus RX-dead + transient 5 GHz TX — the same reader-vs-RF18 race at runtime: a reader
  pause across deliberate (`scan=False`) tunes + `inject` under `_io_lock`.

Not done: ZeroCD discovery (the card enumerates as a CD-ROM — see Gotchas) and warm reattach.

## Gotchas

**The card hides as a CD-ROM (ZeroCD).** It enumerates as USB mass-storage and must be mode-switched
to the Wi-Fi PID `0bda:c820` before any driver binds — so a freshly-plugged card shows a CD-ROM and
Wifit3 finds nothing until the discovery layer handles the switch (a manager-level gap that hits most
Realtek USB adapters). The offline port and gate are unaffected; the pcap was captured in Wi-Fi mode.

**`_dc_cancellation` is deliberately not called** (`dm.phy_init_haldm`). The cal stops then restarts
the 320 MHz BB clock (`_stop_ck320`, `0x8b4[6]`) for its DC measurement, and that restart
intermittently fails to re-lock the demod → `RXFF_PTR=0`, OFDM false-alarm flood, dead RX on both
bands ~half of cold boots. Its DC compensation is unneeded here — skipping it *raises* the beacon
rate. Don't re-enable without re-validating on hardware; the byte-gate can't catch it (every wire op
matches the vendor — the failure is the analog clock-restart transient, not a register value).

**2.4 GHz needs a band-bounce prime, with the reader quiet.** The cold ch1 tune never makes the synth
actually jump TO 2.4 GHz (it runs `_switch_band` but the LO doesn't re-lock on the premature,
pre-antenna-switch tune), so RF18 BIT16 reads stuck-SET and every 2.4 GHz frame fails CRC. Only a
real 5→2.4 GHz band switch re-locks the LO — a direct `RF18[16]=0` write does not (a reverted
regression). `driver._prime_2g_band` bounces 2.4→5→2.4 after cold init; crucially the band-switch
RF18 write is DROPPED if the bulk-IN reader runs concurrently (`_switch_channel` re-writes the stale
BIT16=1), so it STOPS the reader across the bounce, then restarts it. Continuous hopping hides this
(a later switch lands); only a fixed-2.4 GHz session exposes it.

**The byte-gate is blind to timing and to read-modify-write correctness.** `verify_pcap` replays the
*captured* read values, so a dropped settle delay, or an RMW that's only wrong when the real chip
reads differently, both PASS — exactly how the dc_cancellation ck320 and the power-seq LDO-settle
bugs survived byte-faithful ports. It PASSES while EXCEPTING (and reporting) the 91-op
`_dc_cancellation` block we skip; the except is signature-checked (the unique `0xc10` write) so it
can't mask a real divergence.

## Orientation

Start at `bringup.cold_bringup` — init → power seq → firmware → MAC → BB → RF, in kernel order.
Channel tuning is `chan.set_channel` (band-switch sub-step only when the band changes). RSSI is
`rx.decode_rssi` — jgr2 phystatus (decoding it as Jaguar-1 was an early mistake). The two
hardware-bug fixes live in `dm.phy_init_haldm` (the skipped cal) and `driver._prime_2g_band`. Names
match the vendor C, so grep the bundle's `driver-source/` to cross-reference.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate (PASS; excepts the skipped dc_cancellation block).
- `scripts/diag/beacon_watch.py` (+ `beacon_watch_usbcap.py`) — live beacons/s vs the kernel baseline.
- `bringup_hop.py` — continuous dual-band RX-health check, GOOD/DEAD per launch.
- `dc_ab.py` / `dc_steps.py` — the A/B harnesses that pinned the dc_cancellation ck320 bug; kept for re-validation.

## Debug log

### 2026-07-08 — runtime RF18-vs-reader race: focus RX-dead + transient 5 GHz TX

The cold-prime race (a concurrent bulk-IN reverts the `_switch_channel` RF18 read-modify-write) also
bit at runtime, where only `_prime_2g_band` guarded it — one cause, two symptoms:

- **Focus tune goes RX-dead** (fix a 5 GHz target while hopping 5 GHz): the one-shot `set_channel`
  RF18 write was reverted by the reader, and with no further hop nothing re-landed it (hopping
  self-heals, a fixed session doesn't — the cold-prime signature). `set_channel` now pauses the
  reader across a deliberate (`scan=False`) tune; the hop path (`scan=True`) skips it (no per-hop
  drain), and no RX is lost — the write already runs with TRX stopped.
- **Transient 5 GHz TX** (PMKID/deauth needing retries): `inject_frame` held no lock, so a frame
  could bulk-OUT mid-tune onto a stranded synth; it now runs under `_io_lock`.

The vendor doesn't hit either: its RX is URB-based (no blocking reader to contend with the RF SIPI
read) and TX is serialized via `setch_mutex` + the command thread — so the reader pause is our
blocking-reader equivalent and the inject lock matches that discipline. `RxReaderThread` gained an
opt-in `pause()`/`resume()` (8821cu-only). HW: a `scan=False` sweep of five 5 GHz channels (same-band
5→5 focus tunes) caught beacons on all, 0 dead / 2 runs; user-confirmed reliable live TX, no RX-rate
impact.

### 2026-06-24 — active-monitor (HW-ACK forged MAC)

`FAKE_MAC` UNIMPLEMENTED → SPOOFABLE. `enter/exit_active_monitor` re-point REG_MACID (0x0610)
to the chosen MAC and back to the EFUSE MAC — MAC-only, like every Realtek sibling: the
accept-all monitor RCR (`_MONITOR_RCR` AAP) already HW-ACKs `RA==REG_MACID`, no RCR flip. Both
run under `_io_lock`, offloaded, like `set_channel` / the watchdog tick. Cold path untouched
(byte-gate still PASS). HW-confirmed: WPS-PBC completes against an ACK-strict AP (~25 EAPOLs).

### 2026-06-24 — RX fixed on both bands

The cross-band coin toss (~30–60% of cold boots dead on both bands, `RXFF_PTR=0` with the demod
false-alarm-flooding) was `_dc_cancellation`. The RF/MAC register state was byte-identical
good-vs-dead, so a subtractive A/B (`dc_steps.py`, monkeypatching one analog step out at a time)
pinned it to the cal's ck320 (320 MHz BB clock) stop/restart: skipping only that toggle restores RX,
skipping the LNA or 3-wire does not, and a settle delay doesn't rescue it — the clock-restart
transient itself, not a missing wait. Fix: stop calling `_dc_cancellation`; its DC comp is unneeded
and skipping raises the beacon rate. (Eliminated along the way, all with hardware A/Bs: the DC-comp
output, IQK — `bNeedIQK` is never set in monitor mode — timing/pacing, reader-start ordering,
BT-coex, the interrupt/C2H channels, and CCK value handling.)

Fixed 2.4 GHz separately: a non-hopping ch1 session decoded 0 beacons because the cold tune leaves
RF18 BIT16 stuck-set and `_switch_channel` re-asserts it from a stale read. Restored a 2.4→5→2.4 band
bounce (`_prime_2g_band`) to re-lock the LO, and STOP the reader across it so the band-switch RF18
write isn't dropped by a concurrent bulk-IN read. Fixed-ch1 then reads ~6.5 beacons/s.

### 2026-06-24 — power-seq LDO settle

`pwrseq._run_table` treated `_CMD_DELAY` as a no-op, dropping the 1 ms LDO settle `CARDEMU_TO_ACT`
carries after the `0x20[0]=1` power enable (vendor `halmac_common_88xx.c:3078`). A sleep emits no
wire op, so the byte-gate never saw it missing. A real fix (the vendor does it), but NOT the
coin-toss cure — that was dc_cancellation.

### 2026-06-23 — first 2.4 GHz light + RSSI

Cold boot reproduced byte-for-byte but 2.4 GHz RX was dead while 5 GHz worked — pinned to RF18 bit16
(perfectly correlated with CRC failure). Same session: RSSI had been decoding with a Jaguar-1 borrow;
switched to the real jgr2 format and it reads sane (−60 to −84 dBm).
