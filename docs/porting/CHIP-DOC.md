# The chip reference doc

Every chip dir ships a `<CHIP>.md`: the facts about the port you can't get faster by reading the
code. Facts only — verifiable by a human or an LLM. No assumptions, no "we thought X", no
cautionary tales written as suspense, no anthropomorphizing. If a line isn't verifiable, or a
reader would learn it faster from the code, cut it. Use bullets where the content is a list; one
line per bullet.

`chips/rtl8814au_dkms/RTL8814AU_DKMS.md` is the worked example.

## Sections, in order

**Captured Wireless Card** — bands, TX/RX chains, exact device model, USB link speed; `VID:PID`;
where the captures live.

**Linux Driver Source** — upstream repo link; type (mainline / DKMS); exact commit hash (+ version,
date); where it's vendored in-repo. Pin the commit, not just a version — it makes a `file:line`
citation resolvable.

**Python Port Details** — `VID:PID` and how the driver is selected; a one-line status (what works
on hardware, what the verify gate proves); related ports; then a short **Non-obvious in the port**
bullet list — the few implementation facts that would cost a maintainer time. Not a catch-all;
only what bites.

**Known Problems** — bulleted. Each bullet: what breaks, when, and the current known state.

**Driver Entry Points** — feature → where to start reading, one bullet each. Names match the vendor
C, so the name is the cross-reference.

**Scripts** — the reusable diagnostics, grouped, one line each. Mark the one-offs that are safe to
delete.

**Debug log** — dated, append-only, at the bottom. One entry per durable finding: the fact and how
it was established. No "we suspected X, turned out Y" essays, no op-offset progress stamps, no
"VERIFIED" stamps. When a finding hardens into a caveat, move it up into Known Problems or the
code; keep a dated entry only while it still helps (an open problem plus what's been ruled out).

## Skeleton

```
# <CHIP>

## Captured Wireless Card    card, VID:PID, captures dir
## Linux Driver Source       repo, type, pinned commit, vendored path
## Python Port Details       VID:PID + selection, status, related ports, non-obvious traps
## Known Problems            what breaks, when, current state — bulleted
## Driver Entry Points       feature -> module/function, one bullet each
## Scripts                   reusable diagnostics, grouped, one line each
## Debug log                 dated, append-only; durable findings, no essays
```
