# Wifit3 — Known Bugs & QoL

Forward-looking. Defects and quality-of-life nits in *existing* behavior — things
to **fix**. New capabilities to **build** live in `FEATURES.md`; release-gating
work in `RELEASE-PLAN.md`; current per-card state in `../VERIFICATION.md`;
tech-debt / de-vibe (ugly-but-working code) lives in `RELEASE-PLAN.md` § Phase 5.

Tracked in-repo on purpose — offline, greppable, versioned alongside the code
that has the bug. When the repo goes public, GitHub Issues becomes the inbox for
community-filed reports; this stays the curated list.

Ordering is rough priority.

---

## Focus-entry channel tune sometimes doesn't take (0 beacons until re-enter)

Entering Focus on an AP occasionally shows 0 beacons/s; exiting to Scanner and
re-entering Focus on the same target then works (8–9/s). Confirmed cross-family —
RT3572 (Ralink) and MT7610U (MediaTek) — so the bug is in the **shared
Focus→stop-hop→`set_channel` path** (`wlan/interface.py` / `ui/screens/focus.py`),
not a driver. Likely a race/ordering issue: the channel set on Focus entry is
lost or overridden by the channel-hopper teardown, so the first tune doesn't
stick. Repro: Focus a known AP, watch for 0 beacons, then Focus→Scanner→Focus.

## Beacon count truncates past 10k

`10512` renders as `0512`. Auto-size the BEACONS column without breaking
right-alignment.

## WPS PBC auto-invade can monopolize the radio on timeout (Focus)

PBC auto-invade is ON by default and works well, but in Focus a PBC attempt that
times out keeps retrying for the rest of the AP's PBC window, and other attacks
are blocked for that span. Give it manual control — a **Stop PBC** button (and a
**Start PBC** when a window is open) — and/or bound the retry loop so a single
timeout can't hold the radio. Minor; deferred.

---

> Not here: **driver wedge / replug warnings not reaching the UI** is a **release
> blocker** (hardware-failure UX), tracked in `RELEASE-PLAN.md` § 2c — it gates
> the alpha, so it doesn't sit in this backlog.
