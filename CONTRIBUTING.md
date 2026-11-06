# Contributing to Wifit3

Wifit3 is a userland 802.11 auditing tool that talks to USB Wi-Fi cards directly over PyUSB: no
aircrack-ng wrappers, no Scapy. Contributions welcome: chipset drivers, attacks, UI/UX, bug fixes,
docs.

Two things first:

- **Authorized use only.** Wifit3 is for networks you own or are explicitly authorized to test
  (see the README disclaimer).
- **Hardware-damage risk is real.** Userland register + firmware access can permanently brick a
  card. Driver work especially: test on hardware you can afford to lose. We only ever write
  RAM/registers and replay the vendor download path. We never program EFUSE/EEPROM fuses, and a
  PR that does will be rejected.

## Dev setup

This repo uses **uv**: the system `python` doesn't have the project deps.

```
uv sync --group dev          # install (editable + dev deps)
uv run wifit3                # run
uv run pytest                # tests: no hardware needed, USB is mocked
uv run ruff check src/       # lint
uv run pyinstaller wifit3.spec --noconfirm   # build a standalone binary to dist/wifit3[.exe]
```

PyInstaller doesn't cross-compile: build each target on that OS (Windows -> `dist\wifit3.exe`,
Linux -> `dist/wifit3`). Build config lives in `wifit3.spec`.

Don't run `ruff format`. The tree is hand-formatted (~99-col) and the formatter is disabled
repo-wide; lint with `ruff check` and match the surrounding style by hand.

## Porting a chipset

Follow [`docs/porting/METHODOLOGY.md`](docs/porting/METHODOLOGY.md). Before opening the PR, complete
every item in [`docs/porting/FINAL-CHECKLIST.md`](docs/porting/FINAL-CHECKLIST.md).

## Commits & pull requests

- Conventional-commit prefix with a scope: `fix(8822bu): …`, `docs(porting): …`,
  `feat(attacks): …`. One logical change per commit; for driver work, one milestone per commit.
- PR body: what changed and why. For a driver, link the `<CHIP>.md` and note what was
  hardware-tested vs pcap-only.
- Keep real network identifiers out of commits, PR bodies, committed logs, and fixtures (SSIDs,
  BSSIDs, MACs, hostnames): generalize ("the WPA2 AP under test") or redact to `aa:bb:cc:**:**:**`.

## Licensing

By submitting a PR you agree your contribution is licensed under **GPL-2.0-only** (wifit3 is a
derivative of GPLv2 kernel/vendor drivers, not optional), and that you have the right to
contribute it. Any firmware blob under `chips/<chip>/assets/` is not GPL: record its provenance
and redistribution terms (see [FIRMWARE.md](docs/FIRMWARE.md)) and byte-verify it against
linux-firmware.
