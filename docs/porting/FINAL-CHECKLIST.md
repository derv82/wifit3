# Final checklist: before a port ships

Run after METHODOLOGY.md Step 3. Do every item, in order. An item is done when its result is
recorded in the chip doc's `## Final checklist` section: one line per item, with the number, the
sample size and the date (e.g. `ACK, spoofed MAC: 98/100, 1 card, 2026-11-02`). An item that can't
be run gets a line saying why. Never mark an item done without its number.

Test APs, BSSIDs and keys come from the user at runtime. Transmit only at those APs. Never commit
a real BSSID, MAC or SSID.

## 1. The verify gate

- [ ] `verify_pcap.py` calls the driver's own `_bringup()`, `set_channel()` and watchdog tick. It
      does not replay a sequence written inside the script.
- [ ] `connect()` holds only libusb/WinUSB setup, then calls `_bringup()`. `_bringup()` holds
      exactly what the capture shows. The gate calls `_bringup()` directly.
- [ ] The gate prints total, claimed, and waived per label. Claimed + waived = total is the ideal; at least
      the bring-up matches, waiving only what wifit3 doesn't need.
- [ ] Count the vendor requests in each capture with tshark, independently of the gate's extractor.
      The count equals the gate's total.
- [ ] Mutation test, in memory: change one table value, swap two adjacent writes, drop one read.
      Each one fails the gate.
- [ ] Capture paths are command-line arguments. No branching on a file name. No asserts on one
      card's MAC, EFUSE values or chip cut.
- [ ] No flaky bring-up: a failure Linux doesn't have is a port bug. Fix it so the port behaves like Linux.

## 2. Driver vs C, beyond the captures

- [ ] Branches on values read from the card (chip version, cut, package, EFUSE MAC valid/invalid,
      channel plan, crystal, thermal, kfree/TX-gain trim, bulk-OUT size) follow the C for values other
      than the recorded card's. Unit-test the ones the recorded card never takes, and the C's
      channel-14-specific writes. Record: how many such branches, how many unit-tested.
- [ ] No value hard-coded to the recorded card where the C reads it from the chip.
- [ ] No module-level mutable state. Two cards of the same chip in one process must not share state.
- [ ] Restart: start wifit3, see frames, quit, reopen, start the driver again: frames again (warm attach,
      or a cold bring-up every time).
- [ ] RX delivers frames without the FCS.
- [ ] Runtime paths the captures never reach (thermal recalibration, TX power tracking): ported
      from the C and unit-tested. No exception in them can be swallowed by the watchdog loop.
- [ ] `FAKE_MAC` matches the ACK lab result (section 3).

## 3. Hardware

- [ ] Bring-up: 1 cold plug, then 5 warm connects. 5/5 succeed.
- [ ] ACK lab (docs/ACKS.md), `scripts/ack/rx_autoack.py` and `scripts/ack/tx_retries.py`, with a second wifit3
      card as prober and as control. n=100 per phase: own MAC, spoofed MAC with active monitor on,
      spoofed with it off, bogus MAC. Retries to a target nobody owns vs the C's retry limit.

## 4. Grade (docs/GRADING.md, "Per-card checklist")

- [ ] Baselines (scripts/baseline/BASELINING.md): `baseline_linux.py` and `baseline_wifit3.py` on the same card,
      same channels and dwell, the reference AP pinned by BSSID. Invalid if the reference AP is off its channel.
- [ ] `baseline_diff.py --diff ... --ref <reference BSSID>`. Without `--ref` the beacon line measures
      Linux's most-heard AP, not the reference AP.
- [ ] At least 3 sweeps, alternating Linux and wifit3. One sweep of the same driver varied by
      2 beacons/s, so no single sweep decides a gap.
- [ ] TX attacks, WEP where a WEP AP exists, and the 20-min soak, as GRADING.md lists them.
- [ ] Record the grade and its one-line reason.

## 5. Docs

- [ ] Chip doc: terse; every claim matches a recorded result; no claims about removed code; the
      `## Final checklist` section complete.
- [ ] New driver: draft its docs/SUPPORTED-HARDWARE.md section, matrix row (in grade order) and the
      README.md#supported-hardware row, matching the neighbouring rows. Existing driver: change its rows only
      when results differ significantly.

## 6. Credits and licensing

- [ ] **Credits** — add the upstream driver's substantive contributors to docs/CREDITS.md, mapped to the
      new card. Tally commit authorship of the kernel path (and the vendor repo); beware kernel file
      renames (GitHub's `?path=` filter doesn't follow them, so scrape the pre-reorg path too). Drop
      tree-wide mechanical committers; keep the real builders.
- [ ] **Licensing** — wifit3 is GPL-2.0-only (a derivative of GPLv2 drivers). Any firmware blob in
      `chips/<chip>/assets/` is not GPL: record its provenance + redistribution terms from
      linux-firmware's WHENCE and byte-verify the blob (see docs/FIRMWARE.md).

## 7. Ship

- [ ] At the commit being shipped: the gate gives every capture the result recorded in section 1, `uv run pytest` is green,
      `uv run ruff check src/` is clean.
- [ ] No real BSSIDs, MACs or SSIDs in code, tests, docs or commit messages.
- [ ] No plan or milestone wording in code, docs or commit messages.
