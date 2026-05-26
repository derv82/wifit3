# PRE-RELEASE.md

Captured roadmap for taking Wifit3 public (open-source). **This is a parked
backlog, not an active TODO** — nothing here gets worked on until the lead
initiates it. Do not proactively poke about ethics/licensing/release between
unrelated tasks; surface items only when asked or when directly relevant to
work in flight.

Context: the foundation is strong (537 passing tests with mocked hardware, a
layered architecture behind the `WlanDriver` Protocol, per-chip `<CHIP>.md`
ground-truth docs). The gaps below are mostly **documentation, an unreviewed
UI, licensing/ethics decisions, and comment polish** — not structural rot.

---

## 1. Release blockers (must-do before the repo goes public)

- [ ] **Scrub real network identifiers from tracked files + git history.**
  - Home BSSID `aa:bb:cc:dd:ee:01` (NETGEAR2G) is hardcoded as the default
    `--target` / `--target-bssid` in several `scripts/*/test_hw_*.py`, plus
    `scripts/test_dual_nic_sniff.py` and `scripts/rtl8822bu/...`.
  - WEP test router (`channel 6`, key `abcde`) in `scripts/wep/chopchop_probe.py`.
  - `NETGEAR2G` referenced in `NEXT-STEPS.md`.
  - These live in **git history**, not just the working tree — so a clean public
    release needs either a history rewrite (e.g. `git filter-repo`) or a fresh
    squashed/orphan public repo. Decide which **before** flipping public; it's
    the one thing that's hard to undo afterward.
  - ✅ Already safe: `usb_dumps/` (captured OTA traffic) and `data_dumps/`
    (kernel source) are **untracked** (gitignored) — never committed.
  - Going forward this is covered by the `no SSIDs/BSSIDs in commits` rule.

- [ ] **Licensing decision.** The lead's read: these drivers were **not**
  cleanroom-ported — Linux driver source was referenced throughout, and
  aircrack-ng's chopchop was studied (and gave a real perf boost). That likely
  puts the project in the **GPLv2 (derivative of the Linux drivers)** bucket.
  Confirm and add a `LICENSE`. (Not legal advice — record the decision + reason.)
  - [ ] **Firmware blobs in git** — provenance is documented per-chip
    ("pcap-extracted, byte-verified vs linux-firmware"), but verify each blob is
    redistributable and document the terms (linux-firmware `WHENCE`/license).

- [ ] **`README.md`** — see §4.
- [ ] **`CONTRIBUTING.md`** — dev setup (`uv`), how hardware testing works (the
  user-runs-the-card loop), the comment-style rule, where ground-truth docs live.
- [ ] **Authorized-use / ethics notice** — this is an offensive 802.11 tool;
  ship a clear "authorized testing / your own networks only" statement
  (wifite/aircrack-ng both carry one). See also §3.

## 2. Code quality / maintainability

- [ ] **UI review** (`ui/*`) — the lead's biggest blind spot (all Textual work
  was agent-authored). Read-only findings doc first, severity-ranked, each with
  a confidence level. **No speculative hardening** — working code does not get
  5×-complicated for an unprovable edge case; such things are flagged as
  optional with the tradeoff, never silently added. Bias to delete/simplify.
- [ ] **Driver-comparison matrix** — what each of the ~10 drivers does at each
  lifecycle stage (discover / cold-vs-warm connect / set_channel / inject / RX /
  close). Gives a holistic grok without reading every driver cold, **and**
  reveals where a shared base class is actually earned. Doubles as input for
  `ARCHITECTURE.md`. Abstract **surgically and test-backed** afterward — the
  hardware families differ enough (HTC/WMI vs direct-register vs MCU-firmware)
  that a premature base class would be the wrong one. (`RxReaderThread` and
  `chips/rtw88_base/` are examples of abstractions extracted *after* being
  proven duplicated — keep that pattern.)
- [ ] **`ARCHITECTURE.md`** — mostly distillation: the layer stack + module map
  already live in `CLAUDE.md`. Add the `WlanDriver` Protocol contract.
- [ ] **Comment cleanup** — rule codified in `CLAUDE.md` + memory. Date stamps
  remain in ~12 `.py` files (heaviest `rt2800usb/reg_init.py`). Do it
  opportunistically (clean any file you touch) or as a deliberate per-module
  pass with small commits each; `chips/ar9271/protocol/wmi.py` is the
  calibrated reference for the right aggressiveness.

## 3. Safety / hardware (older pre-release ideas, parked)

- [ ] **Long-term hardware burn-in / stress testing** — sustained runs, "try to
  set each chip on fire," catch thermal/USB/stability failures.
- [ ] **Thermal kill-switch** — check whether each chip exposes a thermal
  sensor; if so, a watchdog thread that cuts TX/closes the device past a
  threshold. **Main concern is false positives** (killing a healthy session) —
  needs conservative thresholds + hysteresis, and must fail safe without
  spurious trips.
- [ ] **TX-power / DoS guardrails** — letting users set arbitrary TX power risks
  DoSing the neighborhood or damaging cards. Decide on caps / warnings /
  authorized-use gating. Ties into the §1 ethics notice.

## 4. Docs deliverables (README target)

- [ ] **`README.md`** — keep it simple:
  - one-line pitch + screenshots (the TUI is the selling point)
  - **supported-cards table** (move the user-facing list here from
    `NEXT-STEPS.md` — but preserve `NEXT-STEPS.md`'s supplementary dev context
    rather than deleting it; README = user-facing, NEXT-STEPS = dev status)
  - feature list
  - how it differs from wifite / wifite2 (userland PyUSB, no aircrack
    subprocess wrappers, cross-platform incl. Windows via WinUSB)
  - installation: `uv`, Zadig (Windows/WinUSB) and `rmmod <driver>` (Linux)

## Suggested order

History scrub → driver matrix (also feeds `ARCHITECTURE.md`) →
README/CONTRIBUTING/LICENSE + ethics notice → UI review → opportunistic comment
cleanup. Safety/hardware (§3) on its own track. Each item is independently
shippable; tackle gradually.
