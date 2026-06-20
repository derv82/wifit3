# Wifit3 — Release Plan

- BUGS & QoL: `BUGS.md`
- Hardware/driver work → `PORTING.md`
- Product/UX features → `FEATURES.md`
- Current per-card state → `../VERIFICATION.md`

---

## Core hardware

The DKMS re-ports are the headline hardware work (the cross-family 2.4 GHz RX fix). **Detail,
priority order, vendor sources, and cards-in-the-mail live in `PORTING.md`.** One port per
session; stability day(s) between.

---

## Release blockers

### PII / git-history scrub ⚠️ DO THIS BEFORE ANYTHING GOES PUBLIC

Real-environment identifiers are in the **tree and the git history**. Threat model is
**location**: one leaked BSSID or unique ESSID pins the maintainer's home, so this has to be
airtight, not best-effort. (`usb_dumps/`, `data_dumps/` are already gitignored.)

**What counts as PII** — the *literal* values live ONLY in the untracked `.git/pii-denylist.txt`
the hooks read; never write them into a tracked file (including this one):

- the maintainer's personal name;
- home / known-network **ESSIDs**, including the fixed beacon-rate **reference APs** — these
  sit in `VERIFICATION.md`, the per-chip `<CHIP>.md` docs, and the `scripts/` test-harnesses
  (the inventory grep flags ~a dozen real files, plus binary false-positives to ignore);
- their **BSSIDs**;
- **screenshots** (committed ones already obfuscated) and **videos** (the hard case — audit
  any before they go public).

**Cut-over** (decided — *rewrite* the history in place; keep the full ~1,124-commit graph —
the history is worth more than a single sterile commit, and one commit reads as suspicious):

1. **Rewrite history.** `git filter-repo --replace-text rules.txt --replace-message rules.txt`
   replaces every PII literal with a placeholder across *all* commits — file contents **and**
   commit messages. `rules.txt` maps each literal to a fake (BSSIDs → `aa:bb:cc:dd:ee:ff`,
   ESSIDs → `NETGEAR` / `NETGEAR2G`, the name → `xxxx`) as **word-boundary regex**
   (`regex:\b…\b==>…`) so short tokens don't maul real words. Generate it from
   `.git/pii-denylist.txt`; both stay untracked. SHAs change, but the count, dates, messages,
   and graph are preserved.
   - **Author fields are already clean** — every commit is `derv82` except **one** authored by
     `Claude` (the Focus-redesign PR); fold that into `derv82` with a `--mailmap` in the same
     pass (no-AI-authorship rule).
   - **AI co-authorship trailers** are in **279** commit messages (auto-inserted for a stretch
     before the rule landed). The same `--replace-message` pass drops them
     (`Co-authored-by:.*(Claude|Anthropic)` → removed) — the no-AI-authorship rule applied
     retroactively. The local `commit-msg` hook auto-strips them going forward, so this is a
     one-time backfill.
2. **Verify.** `git log -p --all | grep -iE <literals>` returns **zero**, *and* the firmware
   `.bin` blobs are byte-identical to before (check against the FIRMWARE.md hashes). A short
   ESSID can byte-match a firmware blob and corrupt it on rewrite — confirm none moved, and
   re-run excluding `*/assets/*` if one did.
3. **Preserve history privately.** Push the *rewritten* full history to a **private**
   `wifit3-history` repo; verify it landed.
4. **Publish.** The merged **PR #1** keeps the *pre-rewrite* PII commits alive in
   `refs/pull/1/head` — a force-push can't remove it, and once the repo is public that ref is
   fetchable (and drags its ancestor history along). So **delete and recreate `wifit3`** under
   the same name (drops all PR refs + the 0 forks), push the rewritten history, then flip to
   public. A force-push-in-place would leave that PR ref behind.

**Going forward** — local, untracked hooks block re-introduction: `.git/hooks/pre-commit`
(staged file contents) + `.git/hooks/commit-msg` (commit message) grep against
`.git/pii-denylist.txt`. All live under `.git/` so they're never committed; the denylist holds
the literal terms and is local-only (`--no-verify` bypasses in a pinch). This replaces leaning
on the `no SSIDs/BSSIDs in commits` memory.

### Licensing — decision: GPLv2

Drivers weren't cleanroom-ported (kernel source referenced throughout; aircrack chopchop
studied), so GPLv2 is the natural fit as a kernel-driver derivative + gives back. (Not legal
advice.)

- [ ] Add `LICENSE` (GPLv2).
- [ ] PyInstaller exe: GPLv2 needs source availability — the public repo satisfies it; document.
- [ ] **Firmware blobs:** verify each redistributable via linux-firmware `WHENCE`; one-line
  provenance per chip.
- [ ] Consider shipping **Minnie Drivers** as a separate GPLv2 package.

### Versioning ✅

- `__version__` in `src/wifit3/__init__.py` is the single source of truth; `pyproject` derives
  `[project].version` from it (hatchling dynamic version). `wifit3 --version` reports it.
- Released alpha tagged `v0.1.0a1` (PEP 440); `release.yml` gates the tag against `__version__`
  and marks `aN`/`bN`/`rcN` tags as GitHub pre-releases.
- Semantic versioning: `0.x.x` pre-release, `0.1.x` alpha patches, `0.2.0` for multi-card etc.

### Documentation

Still to add:

- [ ] `CONTRIBUTING.md` — uv setup, the hardware-testing loop, comment-style rule, ground-truth
  doc locations.
- [ ] **Authorized-use / ethics notice** — a clear "your own networks / authorized testing
  only" statement (wifite/aircrack carry one), beyond the README one-liner.
- [ ] `ARCHITECTURE.md` — distill the layer-stack/module-map from `CLAUDE.md` + the
  `WlanDriver` Protocol contract.

### Brick-risk disclaimer ⚠️ (userland USB can damage hardware)

**Release blocker — visible before public.** Userland USB writes registers + the FW-download
path with no kernel driver between us and the silicon, so a bad write/FW-page/power-seq can
brick a card or leave it in an illegal RF state. Amplified by AI-assisted porting (non-
deterministic; the pcap gate catches *unfaithful* sequences, not every *dangerous* one) and
community PRs. Required:
- [ ] **README disclaimer** (reinforced by the LICENSE): *"This software talks to USB Wi-Fi
  hardware at the register level. It can damage or permanently disable ('brick') a device. Use
  at your own risk; no liability for hardware damage."* GPLv2 §15–16 (NO WARRANTY) is the legal
  backstop; this is the human-readable layer.
- [ ] **Porting-safety warning** (`PORTING.md`/`CONTRIBUTING.md`): port at your own risk, test
  on hardware you can lose; risk peaks at FW-download / EFUSE / power-seq.
- [ ] **No-fuse-burn invariant:** we only write RAM/registers + replay the vendor *download*
  path, never program EFUSE/EEPROM fuses — documented so a fuse-write PR is an obvious red flag.
- [ ] (Optional, post-alpha) first-run acknowledgment for the dev/porting tools.

---

## Code quality

- **UI review** (`ui/*`) — biggest blind spot (all agent-authored). Read-only, severity-ranked
  findings doc first; **no speculative hardening** (flag edge-case handling as optional, never
  silently add); bias to delete/simplify.
- **Comment cleanup** — date stamps remain in ~12 `.py` files (heaviest `rt2800usb/reg_init.py`);
  clean opportunistically or per-module. `chips/ar9271/protocol/wmi.py` is the reference for tone.
- **Code-quality audit** — full pass for shortcuts / over-complicated edge cases. Known offender:
  `wlan/packet.py` (802.11 parser) is bare magic numbers (offsets, FC masks, IE tags) → named
  constants + `[WIRE]` cites.

---
