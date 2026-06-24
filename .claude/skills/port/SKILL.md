---
name: port
description: Bring up a USB Wi-Fi chipset driver — port it from the vendor/kernel C and verify against the recorded cold-boot pcap. Use when the user wants to port, bring up, or add support for a new wireless chipset (e.g. "/port rtl8821cu", "port the mt7925", "add support for <card>").
---

# Port a Wi-Fi chipset

You're bringing up a USB Wi-Fi card by re-implementing its Linux driver in Python and verifying
against a recorded capture. The full playbook is `docs/porting/METHODOLOGY.md` — read it before
you start. `docs/porting/CODE-STYLE.md` is how to write the port; `docs/porting/CHIP-DOC.md` is
the reference doc you ship with it.

## The loop

1. Step 0 — ask the user the three questions (mode, bundle, source).
2. Find the doors and carve milestones from the timeline (Step 1).
3. For each milestone: port it from the C source, skipping nothing (Step 2), then verify against
   the pcap — `uv run python scripts/verify_pcap.py <chip>` (Step 3). A divergence is a bug in
   your port; fix the source port, don't waive.
4. Commit each milestone on its own once it verifies.
5. Check RX against a reference AP (Step 4), run `test_hw.py` yourself (Step 5), and walk the
   before-done checklist (Step 6).
6. Keep the chip's `<CHIP>.md` reference current as you go.

Read only this chip's C source while porting — not other chips' drivers (METHODOLOGY explains why).

## When to surface to the user

Run the loop to a stopping point on your own. Surface only when:

- you need a decision you can't make from the source and the pcap,
- you've hit the human gate — live 802.11 TX (injection/deauth) is the user's action, never yours,
- a milestone is ported, verified, and committed, or
- you're genuinely blocked.

Don't narrate progress or "breakthroughs." Finding where a diverging byte came from is just the
work — keep going.

## For other agents

This skill is Claude Code's entry point. The knowledge lives in `docs/porting/` as plain markdown,
so another agent (or a human) follows the same playbook by reading `docs/porting/METHODOLOGY.md`
directly.
