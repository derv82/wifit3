# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

### OUI-Master-Database's device-type classifier has a widespread word-boundary bug

Its `classifyDeviceType()` matches unbounded substrings (`/tesla/i`, `/ring/i`, `/audi/i`,
`/abb/i`, ...) against the full manufacturer name, not whole words. Real, counted false
positives in the current dataset: **1,160 of ~1,372** "Smart Home" entries likely misclassified
via `/ring/i` matching "...ring" inside "Manufacturing"/"Engineering"/etc.; `/audi/i` mismatches
"GN Audio" (an Audio company) into Automotive (342 hits); `/abb/i` mismatches "Abbott
Diagnostics" (Medical) into Industrial (87 hits). We already carry a local
`_CATEGORY_CORRECTIONS` override in `gen_fingerprint_categories.py` for the one entry (Tesla)
that affects our own high-confidence table, but the bug itself is upstream and far bigger than
that one entry.

No PR filed yet. When one is, it should use the same word-boundary fix already applied to our
own `_ICON_OVERRIDES` matching (`wlan/fingerprint.py`) -- same bug class, same fix.

