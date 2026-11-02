# Final checklist: before a port ships

Run after METHODOLOGY.md Step 6. Do every item, in order. An item is done when its result is
recorded in the chip doc's `## Final checklist` section: one line per item, with the number, the
sample size and the date (e.g. `ACK, spoofed MAC: 98/100, 1 card, 2026-11-02`). An item that can't
be run gets a line saying why. Never mark an item done without its number.

Test APs, BSSIDs and keys come from the user at runtime. Transmit only at those APs. Never commit
a real BSSID, MAC or SSID.

## 1. The verify gate

- [ ] `verify_pcap.py` calls the driver's own `_bring_up()`, `set_channel()` and watchdog tick. It
      does not replay a sequence written inside the script.
- [ ] `connect()` holds only libusb/WinUSB setup, then calls `_bring_up()`. `_bring_up()` holds
      exactly what the capture shows. The gate calls `_bring_up()` directly.
- [ ] Ops left unclaimed at the end of the capture fail the run. No anchor searches that skip ops.
- [ ] Every waiver has a label and the C file:line that issued those ops. Linux-only ops only.
- [ ] The gate prints total, claimed, and waived per label. Pass = claimed + waived = total.
- [ ] Count the vendor requests in each capture with tshark, independently of the gate's extractor.
      The count equals the gate's total.
- [ ] Mutation test, in memory: change one table value, swap two adjacent writes, drop one read.
      Each one fails the gate.
- [ ] Capture paths are command-line arguments. No branching on a file name. No asserts on one
      card's MAC, EFUSE values or chip cut.
- [ ] `verify_rx.py` compares decoded fields (type, addresses, SSID) against tshark, and frame
      slicing against the FCS.
- [ ] TX: inject frames through the driver's `inject_frame()` and byte-diff them against the
      capture's bulk-OUT URBs. If the capture has no bulk-OUT URBs, record that and verify TX on air
      (section 3).
- [ ] Every cold-plug capture of this chip passes. At least 2 cold plugs.

## 2. Driver vs C, beyond the captures

- [ ] List every branch on a per-card value: chip version, cut, package, EFUSE MAC (valid and
      invalid), channel plan, crystal, thermal, kfree/TX-gain trim, bulk-OUT size. Each follows the C
      for values other than the recorded card's. Unit-test the branches the recorded card never takes.
- [ ] No value hard-coded to the recorded card where the C reads it from the chip.
- [ ] No module-level mutable state. Two cards of the same chip in one process must not share state.
- [ ] Warm attach: if the vendor probe re-initialises a running chip, `connect()` always runs the
      cold path.
- [ ] RX delivers frames without the FCS.
- [ ] Channels: 1-14 on 2.4 GHz, same as the other ports. Unit-test the C's ch14-specific writes.
- [ ] Runtime paths the captures never reach (thermal recalibration, TX power tracking): ported
      from the C and unit-tested. No exception in them can be swallowed by the watchdog loop.
- [ ] `FAKE_MAC` matches the ACK lab result (section 3).

## 3. Hardware

- [ ] Bring-up: 1 cold plug, then 5 warm connects. Record success count and time to ready.
- [ ] Soak, `scripts/rx/soak.py`: 20 min, ch1-13, watchdog on. Record ticks OK/failed, USB errors,
      thermal readings.
- [ ] ACK lab, `scripts/ack/rx_autoack.py` and `scripts/ack/tx_retries.py`, with a second wifit3
      card as prober and as control. n=100 per phase: own MAC, spoofed MAC with active monitor on,
      spoofed with it off, bogus MAC. Retries to a target nobody owns vs the C's retry limit.
- [ ] TX on air: 100 injected frames, counted by another card in monitor mode.
- [ ] Attacks against a hostapd AP with a random MAC, n=3 each: PMKID, WPS PIN, WPS identity probe,
      broadcom_wps. PMKID and handshake against a test router.
- [ ] WEP, if a WEP test AP exists: `scripts/wep/wep_lab.py` (diagnostic and `--generate`), then ARP
      replay, ChopChop, and PTW recovering the key.

## 4. Baselines (Kali, scripts/baseline/BASELINING.md)

- [ ] `baseline_linux.py` and `baseline_wifit3.py` on the same card, same channels and dwell, the
      reference AP pinned by BSSID. Invalid if the reference AP is off its channel.
- [ ] `baseline_diff.py --diff ... --ref <reference BSSID>`. Without `--ref` the beacon line measures
      Linux's most-heard AP, not the reference AP.
- [ ] At least 3 sweeps, alternating Linux and wifit3. One sweep of the same driver varied by
      2 beacons/s, so no single sweep decides a gap.

## 5. Replacing an existing port (a contributor's, or mainline → DKMS)

- [ ] Run the old driver through the new gate. For each divergence: op#, the fixing commit, and
      whether it changes bytes on the wire or only the gate's structure.
- [ ] Repeat sections 3 and 4 on the old driver, back to back with the new one, with sample sizes.
- [ ] List the old card's EFUSE/chip-version differences from the recorded card, and a short test
      list for the old card's owner.

## 6. Docs and grade

- [ ] Chip doc: terse; every claim matches a recorded result; no claims about removed code; the
      `## Final checklist` section complete.
- [ ] Grade per docs/GRADING.md from the results above. Record the grade and its one-line reason.
- [ ] Draft the docs/SUPPORTED-HARDWARE.md section, its matrix row (in grade order) and the
      README.md#supported-hardware row, matching the neighbouring rows.

## 7. Ship

- [ ] At the commit being shipped: the gate passes on every capture, `uv run pytest` is green,
      `uv run ruff check src/` is clean.
- [ ] No real BSSIDs, MACs or SSIDs in code, tests, docs or commit messages.
- [ ] No plan or milestone wording in code, docs or commit messages.
