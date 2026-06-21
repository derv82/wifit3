# Contributing to Wifit3

Wifit3 is a userland 802.11 auditing tool that talks to USB Wi-Fi cards directly over PyUSB —
no `aircrack-ng` wrappers, no Scapy. Contributions welcome: new chipset drivers, attack
implementations, UI/UX, bug fixes, docs.

Two non-negotiables first:

- **Authorized use only.** Wifit3 is for networks you own or are explicitly authorized to test
  (see the README Disclaimer).
- **Hardware-damage risk is real.** Userland register + firmware access can permanently disable
  ("brick") a card. Driver work especially: test on hardware you can afford to lose.

## Dev setup

This repo uses **`uv`** — the system `python` does not have the project deps.

```
uv sync --group dev          # install (editable + dev deps)
uv run wifit3                # run
uv run pytest                # tests — no hardware needed, USB is mocked
uv run ruff check src/       # lint
```

**Never run `ruff format`.** The tree is hand-formatted (~99-col, multi-per-line collections)
and is deliberately not `ruff format`-clean; the formatter is disabled repo-wide. Lint with
`ruff check` only, and match the surrounding style by hand.

## Code style — comments are a closed allowlist

Only four kinds of comment belong in the code:

1. **Docstrings** — every function/class: *what + why*, not how.
2. **Citations & magic-value explainers** — `[SRC]/[WIRE]/[HW]` references, and any bare literal
   (register address, byte constant, FC mask, offset) a named constant doesn't already explain.
   A comment *or* a named constant — whichever reads better; don't extract a constant when the
   inline literal is clearer.
3. **Phase landmarks** — a one-line `# Warm path` naming a phase, in functions that actually
   have several.
4. **Surprise-why** — one line where code does something a competent reader wouldn't expect (an
   ordering constraint, a workaround). The *why*, never the *what*.

Anything else — restating a line, narrating control flow, per-branch labels, "now we…", or notes
about the editing history — is noise; leave it out. Naming carries more than comments do.

Heavy vs light by area: `engine/attacks/**` should explain the attack mechanism thoroughly (a
ChopChop or fragmentation step is unreadable as raw bytes); `chips/**`, the 802.11 logic in
`wlan/**`, and register-touching scripts cite source/wire densely; `ui/**` is essentially
docstrings only. A little over-commenting from a human is fine — we'd rather see your reasoning
than not.

## Contributing a new chipset driver

The porting playbook is **[`planning/PORTING.md`](planning/PORTING.md)**. Read it top to bottom —
it's written in our coding agent's voice, but the procedure, the code hygiene, and the gotchas
are identical for a human port.

Your PR must include:

- **The `<CHIP>.md` port reference.** Every driver ships one (template + rules at the end of
  PORTING.md): silicon, the kernel↔Python **entry-points table**, hot paths, scripts, caveats,
  known issues. This isn't bureaucracy — it's how the port gets reviewed. The entry-points
  "doors" table is the map a reviewer uses to confirm the port does what it claims, so treat it
  as the centerpiece. Citations (`[SRC]`/`[WIRE]`) carry each claim; pointers (`path:line`), not
  paraphrase.
- **Evidence it works.** At minimum the offline pcap verification (PORTING.md Step 3) against the
  chip's cold-boot capture. Live RX/attack results are welcome, but the maintainer re-runs
  hardware checks regardless.
- **Credits.** Add the upstream driver's substantive contributors to `CREDITS.md` (see PORTING.md
  → Housekeeping).

**Safety.** Bring-up writes registers and replays the vendor firmware-download path directly to
the silicon — test on a card you can afford to lose. The **no-fuse-burn invariant** holds for all
contributions: we write only RAM/registers and replay the vendor *download* path; we never program
EFUSE/EEPROM fuses. A PR that adds a fuse/EEPROM write will be rejected.

## Commits & pull requests

- **Commit messages** use a conventional-commit prefix with a scope: `fix(8822bu): …`,
  `docs(porting): …`, `feat(attacks): …`, `chore: …`, `test: …`. One logical change per commit;
  for driver work, one milestone per commit — don't batch bring-up steps.
- **PR body** — what changed and why. For a driver, link the `<CHIP>.md` and note what was
  hardware-tested vs pcap-only.

## Licensing & provenance

By submitting a pull request you agree that:

- Your contribution is licensed under **GPL-2.0-only**. Wifit3 is a derivative work of GPLv2
  kernel/vendor drivers — this is not an optional choice.
- You have the right to contribute it — in particular, it contains no proprietary or vendor source
  you don't have the right to redistribute.

Any firmware blob shipped under `chips/<chip>/assets/` is **not** GPL: record its provenance and
redistribution terms (see [FIRMWARE.md](FIRMWARE.md)) and byte-verify the blob against
`linux-firmware`.
