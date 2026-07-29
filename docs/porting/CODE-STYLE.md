# Code style (ports)

A port is a translation, not a redesign. The closer it tracks the source, the easier it is to
verify, debug, and update against a newer kernel.

## Names match the source

Keep the C names. A constant, variable, or function named the same as its kernel/vendor
counterpart *is* its own cross-reference. Anyone can grep the source and land on it. Don't rename
`BIT_FWDL_CHK_RPT` to something "clearer," and don't restructure a helper into something prettier
than the original. When a name can't carry the link (a value with no named symbol, or a place
where you had to split or merge a helper), add a `file:line` pointer. That's the only citation a
port needs.

Never type a register address or bitfield from memory. List the symbols you need, grep them out of
the source in one pass, and paste each line verbatim. `0x07a8` vs `0x0708` is a one-digit bug
that reads on hardware as a USB timeout, not a constants error. Copy the `BIT(n)` literal too;
adjacent symbols often have non-adjacent bit positions.

## Comments

Comments are a small, closed set. For the agent it's a ceiling: when unsure, omit; a name carries
more than a comment. The kinds that belong:

- **A docstring** where the name doesn't already carry it: the non-obvious what or
  why, never the how. Omit it on a trivial or self-evident function (a good name
  beats a restated signature). One line unless the why genuinely needs more; no
  runon or cryptic shorthand.
- **A pointer or magic-value note** — a `file:line` where the name doesn't carry it, or a bare
  literal a named constant wouldn't make obvious (a comment or a named constant, whichever reads
  better).
- **A phase landmark** — a one-line `# Cold boot: fw → init → monitor`, only in a function with
  two or more such phases.

Everything else is noise: restating a line, narrating control flow (`# loop over APs`), per-branch
labels, "now we…", and session history (no "we used to," dated stamps, commit hashes, one-off dump
percentages). A cross-reference between files only when they actually share code or a family, not
"I fixed the same bug here."

By area: `campaigns/**` may explain an opaque byte/XOR step (the why the crypto needs,
not a lecture); `chips/**`, `wlan/**`, and register-touching `scripts/<chip>/*` cite the source
densely; `ui/**` is docstrings plus the occasional magic-value or non-obvious-why note
(no C source to cite there), never inline narration. Dated history lives in the chip doc, not the `.py`.
