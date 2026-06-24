# Contributing to Wifit3

Wifit3 is a userland 802.11 auditing tool that talks to USB Wi-Fi cards directly over PyUSB — no
aircrack-ng wrappers, no Scapy. Contributions welcome: chipset drivers, attacks, UI/UX, bug fixes,
docs.

Two things first:

- **Authorized use only.** Wifit3 is for networks you own or are explicitly authorized to test
  (see the README disclaimer).
- **Hardware-damage risk is real.** Userland register + firmware access can permanently brick a
  card. Driver work especially: test on hardware you can afford to lose. We only ever write
  RAM/registers and replay the vendor download path — we never program EFUSE/EEPROM fuses, and a
  PR that does will be rejected.

## Dev setup

This repo uses **uv** — the system `python` doesn't have the project deps.

```
uv sync --group dev          # install (editable + dev deps)
uv run wifit3                # run
uv run pytest                # tests — no hardware needed, USB is mocked
uv run ruff check src/       # lint
```

Don't run `ruff format`. The tree is hand-formatted (~99-col) and the formatter is disabled
repo-wide; lint with `ruff check` and match the surrounding style by hand.

## Porting a chipset

With Claude Code, type `/port <chip>` — the skill walks the whole process. With another agent, or
by hand, follow [`docs/porting/METHODOLOGY.md`](docs/porting/METHODOLOGY.md); it's the same
playbook. The short version: port from the vendor/kernel C, keep the C names (a matching name is
its own cross-reference — add a `file:line` only when it can't carry the link), verify each
milestone against the cold-boot pcap, and ship the chip's `<CHIP>.md` reference. Code style is
[`docs/porting/CODE-STYLE.md`](docs/porting/CODE-STYLE.md); the chip-doc template is
[`docs/porting/CHIP-DOC.md`](docs/porting/CHIP-DOC.md).

Your PR should include the `<CHIP>.md` reference, evidence it works (at minimum the offline pcap
verification), and credits for the upstream driver's authors in CREDITS.md.

## Commits & pull requests

- Conventional-commit prefix with a scope: `fix(8822bu): …`, `docs(porting): …`,
  `feat(attacks): …`. One logical change per commit; for driver work, one milestone per commit.
- PR body: what changed and why. For a driver, link the `<CHIP>.md` and note what was
  hardware-tested vs pcap-only.

## Licensing

By submitting a PR you agree your contribution is licensed under **GPL-2.0-only** (wifit3 is a
derivative of GPLv2 kernel/vendor drivers — not optional), and that you have the right to
contribute it. Any firmware blob under `chips/<chip>/assets/` is not GPL: record its provenance
and redistribution terms (see [FIRMWARE.md](FIRMWARE.md)) and byte-verify it against
linux-firmware.
