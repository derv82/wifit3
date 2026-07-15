# Porting gotchas

Recurring traps from bringing up drivers in this tree. Each is a real bug that cost hardware
cycles; the symptom is spelled out because that's how you'll rediscover it. Companion to
[`METHODOLOGY.md`](METHODOLOGY.md) (the playbook), [`CODE-STYLE.md`](CODE-STYLE.md) (how to write
the port), and [`CHIP-DOC.md`](CHIP-DOC.md) (the per-chip reference you ship).

## Mirror the kernel; the pcap verifies, it isn't the init source

Port functions and tables verbatim from the kernel/vendor C. The pcap is for *verification*, not a
source of init bytes — replay-based init is a black box that doesn't scale to siblings or survive a
kernel bump. Make the first milestone the smallest verifiable handshake (chip-ID read, FW upload +
ACK), and verify each milestone against the pcap before calling it done; "HW test passes" isn't
enough.

**Why:** mt76x0u shipped ~7K SLoC of port-by-vibes before this discipline existed. One dropped MCU
response wedged the chip and we couldn't debug it, because we didn't know which lines matched the
kernel and which had silently diverged.

**How to apply:** treat any claim in a chip's `<CHIP>.md` as a hypothesis unless its evidence is a
specific pcap frame range you can re-verify.

## Over-port: partial ports compile, run, and silently degrade

Partial ports are the most common bug class here. Default to over-porting; cut later if proven
unused. METHODOLOGY Step 2 ("skip nothing by name") is the principle; the concrete forms that bite:

- **Switch/case:** port every branch, even ones this card can't trigger — behind its real runtime
  check, marked `# TODO: verify — untested here`. That's "ported, untested," not skipped, and it's
  what lets a sibling card work.
- **Helper:** port every `*_write` in it, not just the obvious one — helpers bundle register pokes
  that are jointly required.
- **Vtable:** port `init_device` *and* `start`. The "arm the radio" writes (OFDM enable, TXPAUSE=0,
  antenna select) often live in `start`.
- **Struct:** scan past the `#endif`. Fields declared outside an `#ifdef __LITTLE_ENDIAN` block
  still count toward `sizeof` and are easy to miss.
- **Bitfields:** `u8 a:N, b:M;` is one byte (shared storage), not two; `path_agc[2]` of those is
  2 bytes total.

The failure mode is uniform: code runs, the chip misbehaves, and the symptom looks like something
else (wrong RX routing reads as a sensitivity problem, a wrong struct offset as a parser bug).
Hours of debug for a one-line omission.

## Skip comments mispredict which axis breaks

When a port skips a kernel function and leaves a comment predicting the symptom ("if RX looks deaf,
add this back"), the prediction is usually wrong about *which axis* breaks — so the comment becomes
a trap: the real symptom doesn't match, the skip looks irrelevant, and you hunt in the wrong place.

**Why:** mt76x2u skipped `mt76x2_phy_set_txpower_regs` as a "monitor-mode RX simplification"
predicting an RX-sensitivity symptom. The actual symptom was TX-side: sustained injection (WEP ARP
replay / ChopChop / Frag) yielded 0 IVs/s because the AP couldn't hear our frames and deauthed us
as a weak STA. Hours went into ACK-tracking, MAC programming, and frame-format theories before the
skip note — which named TX-power but predicted an RX symptom — turned up.

**How to apply:** "we're in monitor mode" is not a TX skip rationale — the moment `inject_frame`
runs (deauth, fake-auth, replay), every TX-side helper is back in scope. When debugging a TX/RX
symptom, read the chan/init skip list first and diff what the kernel writes vs what we write during
that operation. If you skip a function, describe *what writes it makes*, not the symptom you predict.

## Idle-poll before bulk per-station table clears

When an init bulk-clears per-station state tables (mt76's WCID 256-iter / SKEY 64-iter loops,
similar elsewhere), the idle poll the kernel calls between `mac_setaddr` and the clear loop is
load-bearing, not defensive padding.

**Why:** without it, a subset of the 500+ writes race the chip's in-flight TX/RX engines and don't
take effect. The raw-inject slot (wcid 0xFF in mt76) is the one that suffers — the first
fake-auth/injection misbehaves, but the chip converges after 5–10 s idle, so the second attempt
looks fine. The symptom reads like an AP timing issue or warm-up, but it's our init not finishing
cleanly.

**How to apply:** look for a `wait_for_*_idle` / `poll_msec(MT_MAC_STATUS, ...)` between
`mac_setaddr` and the table clears, and port it — don't defer it because "the chip should be idle
by now." If the symptom is "first inject fails, retry reliably succeeds," suspect a missed idle poll
before a bulk table clear, not an AP timer or PA warm-up. (mt76x2u: `mt76x02_wait_for_txrx_idle`,
mt76x02.h.)

## Warm reattach, and the cold-only-init no-op trap

When `connect()` hits a chip already running from a prior session: detect warm cheaply (read
registers that survive between processes — e.g. rtw88: `REG_MCUFW_CTRL` FW_READY + `REG_CR`
MACTXEN|MACRXEN) and skip the whole bring-up. Reattach lightly (re-claim the interface,
`clear_halt` the bulk pipes, start RX polling), then smoke-test with a ~1.5 s bulk-IN read; if it's
silent, surface a clear "please replug" rather than retrying.

**Why (WinUSB):** the kernel's pwr_off → pre_cfg → pwr_on cycle does not recover a wedged bulk-IN
pipe between userland sessions — `clear_halt`, `dev.reset()`, the full pwr_seq cycle, and pipe
drains all failed. The silicon is fine (registers respond); the host controller's view of the pipe
is stuck in a way userland can't clear (the kernel only survives it by continuously resubmitting
URBs, which our sync reads don't).

**TRAP (cost ~4 hardware round-trips):** the warm path skips post-FW init, so a fix placed in
cold-only init silently no-ops on a warm chip — and the chip stays warm across `uv run` sessions
until an unplug/replug. We "fixed" the monitor RX filter three times in cold-only init and saw no
change, because none of it ran. So if a config change "has no effect across runs," suspect warm
first. Anything that must hold in steady state (RX filter, monitor mode, address match) belongs in
the common attach tail, reasserted on both warm and cold; only hardware-state changes (FW upload,
power seq) stay cold-only.

## Cross-driver RX gap classes

Recurring gaps a monitor-mode RX path hits across drivers. Most are fixed in-tree; treat this as a
watch-for when bringing up or debugging a driver's reception. When in doubt on a specific driver,
verify monitor RX against its airmon-ng pcap rather than theorizing.

1. **RX poll-loop starvation** — reading and parsing on the asyncio loop drops frames while the UI
   is busy (10 Hz Focus update, scanner render): no `dev.read` is posted, so the dongle's RX FIFO
   overflows. Signature: ~7 beacons/s vs airodump's ~10, ~1-in-5 4-way capture in Focus, yet a
   no-TUI `test_hw.py` catches everything. The fix is the shared `chips/rx_reader.py`
   `RxReaderThread` — a dedicated reader keeps a URB posted at all times and hands raw buffers to
   the loop via `call_soon_threadsafe` (parse + callback stay on the loop thread); it backs ~8
   drivers. **Start the reader before RX-enable** — on 8814au, enabling MAC RX and only then
   starting the reader left an undrained window that latched a wedge surviving until replug (connect
   succeeds, a few frames arrive, then nothing). "A few frames then permanent stop" is a latch, not
   throughput loss; audit any `RxReaderThread` driver for reader-start-after-RX. Watch-for, not a
   mandate to convert every driver (ar9271's two read tasks and the unported mt76 chips aren't
   believed to need it); single-reader ordering is the fix, not async / multi-URB.
2. **ToDS / monitor filter** — an STA-mode RCR that isn't promiscuous shows only M1/M3, never the
   full 4-way. The monitor RCR must accept client→AP (ToDS) traffic.
3. **QoS header pad breaks FCS validation** — hardware inserts `hdrlen & 3` pad bytes after a QoS
   MAC header to 4-byte-align the payload, but the over-air FCS excludes them. A driver doing its
   own CRC32 check must strip the pad first, or every QoS frame (all downlink-unicast plus the whole
   4-way) silently FCS-fails while beacons pass. Symptom: scanning works, zero passive handshakes.
4. **FCS trailer in saved pcaps** — fixed generically in `engine/pcap.write_pcap` (`_strip_fcs`
   drops the last 4 bytes only when they're a valid CRC32 of the rest). Don't re-fix this per-driver
   in `parse_rx_frame`: `raw` is the MPDU as the chip delivered it; normalization happens once, at
   the pcap boundary.
5. **RX callback delivers FCS-stripped MPDUs** — every driver strips the trailing 4-byte FCS before
   `register_rx_callback`, so length-sensitive consumers (WEP ARP detect, ChopChop ICV, Frag seed,
   WPS WSC HMAC) can trust `len(frame)` and `frame[-N:]` as the on-air MPDU, no per-driver
   branching. Strip it at the lowest RX layer (the iterator / WMI demux) before the frame leaves
   `chips/`. A 2026-05 campaign found 6 of 10 drivers leaking the FCS, silently breaking all four
   attacks. `RTL8821AU` has its own `iter_bulk_frames` (`chips/rtl8821au/rx.py`), not the shared
   `rtw88_base/rx_common`, so a descriptor-level fix to the rtw88 family must touch it separately.
   Diagnostic: the FCS-presence CRC32 tally (`git show 2c12861`) pins at 100% has_fcs pre-fix, ~0%
   post-fix.

## Verify's operational phase: dispatch async producers by opener

`verify_pcap` is a single monotonic cursor over the wire. Deterministic init replays as one linear
walk, but the operational phase interleaves async producers whose ordering is wall-clock /
traffic-driven: channel hops, the SW-LED BlinkTimer, the phydm watchdog tick, the BT-coex
periodical.

The pattern (built in `rtl8814au_dkms`, reused in `rtl8821cu_dkms`): a `Walk` holds the cursor and
the real transport; the operational loop peeks the next op, matches it to each producer's unique
*opener* op — its distinctive first register touch (`read_rf 0x18` = hop, `read 0x4e` = LED,
`read 0x210` = watchdog) — dispatches to that producer's real driver handler, and advances the
cursor by exactly what the handler consumed. The first op no handler opens is the frontier.

A strict positional cursor breaks the instant two producers interleave (a timer fires mid-hop);
dispatch-by-opener reproduces each with real driver logic in any order, nothing stripped. The
instinct to call an interleaved op "non-deterministic / unportable" is almost always wrong — it's
real driver code; find its opener. Carry per-producer state (DIG IGI, CCK-PD level + MA, LED phase,
HMEBOX index, NHM/CLM period) so first-tick-vs-steady writes suppress correctly. A single genuinely
traffic-driven cosmetic bit (the LED at `0x4e[3]`) may be value-bypassed, but only with lead
approval and scoped to that one bit.
