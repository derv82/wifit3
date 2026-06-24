# The chip reference doc

Every chip dir ships a `<CHIP>.md` — a short reference a maintainer or the next agent reads before
touching the driver. Write it for someone who would reject a wall of text in a PR.

The test for every line: **could a reader get this faster by reading the code?** If yes, leave it
out. The doc is for what *isn't* in the code — what works, what's broken, and the non-obvious
things that cost you.

Style: prose for anything that explains; bullets only for genuinely list-shaped things, and a
bullet is one line. If a thought needs two lines, write it as a sentence or cut it. The reference
body (everything above the dated log) should fit on a screen — if it's longer, you're transcribing
the code.

What goes in, and nothing else:

- **Status** — what works, what's broken, what's untested, on hardware. The thing you can't get
  from the code.
- **Gotchas** — the non-obvious, hard-won facts (a ZeroCD enumeration, a single RX-gating bit,
  "this card is marginal even under the vendor driver"). If you'd learn it by reading the code, it
  doesn't belong.
- **Orientation** — a one-line silicon summary and a handful of pointers to where to start
  reading. Names match the C; that's the cross-reference. Not the whole call graph.
- **Scripts** — the reusable diagnostics, one line each.
- **Debug log** — dated, append-only, at the bottom. The "why we found it" stories live here,
  once. No op-offset progress stamps ("GREEN @ 8033"), no "VERIFIED" stamps, ever.

Skeleton:

```
# <CHIP>            one line: family, kind of port, source

## Status          works / broken / untested — on hardware
## Gotchas         the non-obvious, hard-won stuff
## Orientation     silicon one-liner + a few pointers into the code
## Scripts         reusable diagnostics, one line each
## Debug log       dated, append-only; the stories, kept out of the reference body
```

Write it as the port stabilizes, not every milestone. When a dated finding becomes a durable
caveat, promote it up into Gotchas; the dated entry keeps the "why we found it."
