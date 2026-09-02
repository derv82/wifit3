#!/usr/bin/env python3
"""Show what each fingerprint Rule matches across the shipped vendor table, so a human can spot
over-matching regexes before shipping. Reads VENDOR_BY_OUI; no download."""
from __future__ import annotations

import re
import sys

from wifit3.wlan.fingerprint import _GENERIC_EMOJI, _RULES, VENDOR_BY_OUI

_SAMPLE = 15


def _distinct_vendors() -> list[str]:
    return sorted(set(VENDOR_BY_OUI.values()))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    vendors = _distinct_vendors()
    print(f"{len(VENDOR_BY_OUI)} OUIs, {len(vendors)} distinct vendor names. Generic = {_GENERIC_EMOJI}\n")
    for rule in _RULES:
        if rule.pattern is not None:
            hits = [v for v in vendors if re.search(rule.pattern, v, re.I)]
            print(f"{rule.emoji}  pattern {rule.pattern!r}  ->  {len(hits)} distinct vendors")
            for v in hits[:_SAMPLE]:
                print(f"      {v}")
            if len(hits) > _SAMPLE:
                print(f"      ... +{len(hits) - _SAMPLE} more")
        else:
            named = sorted({VENDOR_BY_OUI[o.upper()] for o in rule.ouis if o.upper() in VENDOR_BY_OUI})
            miss = sum(1 for o in rule.ouis if o.upper() not in VENDOR_BY_OUI)
            print(f"{rule.emoji}  {len(rule.ouis)} OUIs  ->  table names: {named or '(none in table)'}"
                  + (f"  ({miss} not in table)" if miss else ""))
        print()


if __name__ == "__main__":
    main()
