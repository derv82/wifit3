# Porting gotchas

Recurring traps from bringing up drivers in this tree. Each is a real bug that cost hardware
cycles; the symptom is spelled out because that's how you'll rediscover it. Companion to
[`METHODOLOGY.md`](METHODOLOGY.md) (the playbook) and [`CHIP-DOC.md`](CHIP-DOC.md) (the per-chip reference you ship).

## Mirror the kernel; the pcap verifies, it isn't the init source

Port functions and tables verbatim from the kernel/vendor C. The pcap is for *verification*, not a
source of init bytes. Replay-based init is a black box that doesn't scale to siblings or survive a
kernel bump. Make the first milestone the smallest verifiable handshake (chip-ID read, FW upload +
ACK), and verify each milestone against the pcap before calling it done; "HW test passes" isn't
enough.

**Why:** mt76x0u shipped ~7K SLoC of port-by-vibes before this discipline existed. One dropped MCU
response wedged the chip and we couldn't debug it, because we didn't know which lines matched the
kernel and which had silently diverged.

**How to apply:** treat any claim in a chip's `<CHIP>.md` as a hypothesis unless its evidence is a
specific pcap frame range you can verify again.

## Over-port: partial ports compile, run, and silently degrade

Partial ports are the most common bug class here. Default to over-porting; cut later if proven
unused. METHODOLOGY Step 2 ("skip nothing by name") is the principle; the concrete forms that bite:

- **Switch/case:** port every branch, even ones this card can't trigger, behind its real runtime
  check, marked `# TODO: verify, untested here`. That's "ported, untested," not skipped, and it's
  what lets a sibling card work.
- **Helper:** port every `*_write` in it, not just the obvious one: helpers bundle register pokes
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

## Wait for the chip where the driver waits

Port every idle poll and ACK wait the C has (e.g. mt76x2u `mt76x02_wait_for_txrx_idle` before its table clears).
Skipped, writes race the chip and some don't land; a replay can't show it, because it has no timing. Symptom:
the first inject fails and a retry succeeds.

## Warm reattach, and the cold-only-init no-op trap

When `connect()` hits a chip already running from a prior session: detect warm cheaply (read
registers that survive between processes, e.g. rtw88: `REG_MCUFW_CTRL` FW_READY + `REG_CR`
MACTXEN|MACRXEN) and skip the whole bring-up. Reattach lightly (claim the interface,
`clear_halt` the bulk pipes, start RX polling), then smoke-test with a ~1.5 s bulk-IN read; if it's
silent, surface a clear "please replug" rather than retrying.

**Why (WinUSB):** the kernel's pwr_off → pre_cfg → pwr_on cycle does not recover a wedged bulk-IN
pipe between userland sessions: `clear_halt`, `dev.reset()`, the full pwr_seq cycle, and pipe
drains all failed. The silicon is fine (registers respond); the host controller's view of the pipe
is stuck in a way userland can't clear (the kernel only survives it by continuously resubmitting
URBs, which our sync reads don't).

**TRAP (cost ~4 hardware round-trips):** the warm path skips post-FW init, so a fix placed in
cold-only init silently no-ops on a warm chip, and the chip stays warm across `uv run` sessions
until an unplug/replug. We "fixed" the monitor RX filter three times in cold-only init and saw no
change, because none of it ran. So if a config change "has no effect across runs," suspect warm
first. Anything that must hold in steady state (RX filter, monitor mode, address match) belongs in
the common attach tail, reasserted on both warm and cold; only hardware-state changes (FW upload,
power seq) stay cold-only.

## Cross-driver RX gap classes

Recurring gaps a monitor-mode RX path hits across drivers. Most are fixed in-tree; treat this as a
watch-for when bringing up or debugging a driver's reception. When in doubt on a specific driver,
verify monitor RX against its airmon-ng pcap rather than theorizing.

1. **RX poll-loop starvation** — reading on the asyncio loop drops frames while the UI is busy (~7 beacons/s
   vs airodump's ~10; `test_hw.py` without the TUI hears everything). Use `chips/rx_reader.py`
   `RxReaderThread`, which keeps a URB posted. On 8814au, starting the reader after MAC RX-enable latched a
   wedge until replug ("a few frames, then nothing").
2. **ToDS / monitor filter** — an STA-mode RCR that isn't promiscuous shows only M1/M3, never the
   full 4-way. The monitor RCR must accept client→AP (ToDS) traffic.
3. **QoS header pad breaks FCS validation** — hardware inserts `hdrlen & 3` pad bytes after a QoS
   MAC header to 4-byte-align the payload, but the over-air FCS excludes them. A driver doing its
   own CRC32 check must strip the pad first, or every QoS frame (all downlink-unicast plus the whole
   4-way) silently FCS-fails while beacons pass. Symptom: scanning works, zero passive handshakes.
4. **RX callback delivers FCS-stripped MPDUs** — strip the trailing 4-byte FCS before the frame leaves
   `chips/`. WEP ARP detect, ChopChop, fragmentation and WPS rely on `len(frame)` being the on-air MPDU.
