---
name: port
description: Bring up a USB Wi-Fi chipset driver: port it from the vendor/kernel C and verify against the recorded cold-boot pcap. Use when the user wants to port, bring up, or add support for a new wireless chipset (e.g. "/port rtl8821cu", "port the mt7925", "add support for <card>").
---

# Port a Wi-Fi chipset

You're bringing up a USB Wi-Fi card by re-implementing its Linux driver in Python and verifying
against a recorded capture. The full playbook is `docs/porting/METHODOLOGY.md`. Read it before
you start. `docs/porting/CODE-STYLE.md` is how to write the port; `docs/porting/CHIP-DOC.md` is
the reference doc you ship with it.

## The loop

1. Step 0 — ask the user the three questions (mode, bundle, source).
2. Find the doors and carve milestones from the timeline (Step 1).
3. For each milestone: port it from the C source, skipping nothing (Step 2), then verify against
   the pcap: `uv run python scripts/porting/verify_pcap.py <chip>` (Step 3). A divergence is a bug in
   your port; fix the source port, don't waive.
4. Commit each milestone on its own once it verifies.
5. Check RX against a reference AP (Step 4), run `test_hw.py` yourself (Step 5), and walk the
   before-done checklist (Step 6).
6. Keep the chip's `<CHIP>.md` reference current as you go.

Read only this chip's C source while porting, not other chips' drivers (METHODOLOGY explains why).

## Scaffolding the chip package

Creating `chips/<name>/`? Two registration rules the `Driver` ABC does not enforce:

- **`__init__.py` declares the VID:PIDs, not the driver class.** It sets
  `SUPPORTED_IDS = [DeviceID(...), ...]` (`from wifit3.models.device_id import DeviceID`) plus a
  `def import_driver(): from .driver import <Class>; return <Class>`, and must NOT import `driver.py`
  at module top (discovery reads the light `__init__` and imports the driver only on a VID:PID match).
  Copy the shape from `chips/rtl8812au/__init__.py`.
- The driver class declares `SUPPORTED_CHANNELS` only; `SUPPORTED_IDS` is not on it. Discovery is a
  pkgutil walk over `chips/*` (no manual registry to edit). If the setup key differs from the package
  dir, or two packages share a VID:PID (the Realtek mainline/DKMS pairs), add a `_FAMILIES` row in
  `device/manager.py`. METHODOLOGY Step 1 has the detail.

## When to surface to the user

Run the loop to a stopping point on your own. Surface only when:

- you need a decision you can't make from the source and the pcap,
- you've hit the human gate: live 802.11 TX (injection/deauth) is the user's action, never yours,
- a milestone is ported, verified, and committed, or
- you're genuinely blocked.

Don't narrate progress or "breakthroughs." Finding where a diverging byte came from is just the
work. Keep going.

## For other agents

This skill is Claude Code's entry point. The knowledge lives in `docs/porting/` as plain markdown,
so another agent (or a human) follows the same playbook by reading `docs/porting/METHODOLOGY.md`
directly.
