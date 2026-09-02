#!/usr/bin/env python3
"""Regenerate ``src/wifit3/wlan/fingerprint_vendors.py`` from Wireshark's ``manuf`` feed.

Client fingerprinting's low-confidence tier (wlan/fingerprint.py) names a client's vendor
without claiming a specific device class -- unlike gen_router_ouis.py (which filters the ~40k-row
IEEE registry down to ~300 router-brand prefixes for a narrow purpose), this keeps every entry:
broad "who made this" coverage is the entire point of the low-confidence tier, so there's no
vendor allowlist to filter against.

Sourced from Wireshark's own ``manuf`` feed rather than the raw IEEE registry: Wireshark
regenerates it weekly straight from IEEE, and -- unlike the flat IEEE MA-L table -- it already
resolves IEEE's finer-grained MA-M (28-bit) / MA-S (36-bit) sub-allocations, so a 24-bit block
IEEE splits between several organizations (e.g. Tesla owns only a nibble of one such block, not
the whole thing) maps to the *actual* owner of each piece instead of one misleading name for the
whole block. Vendor names are still cleaned up here (corporate suffixes stripped, ALL-CAPS
title-cased) the same way as before -- Wireshark's own "long name" column keeps raw suffixes
("Samsung Electronics Co.,Ltd"), its heavily-truncated "short name" column (built for their
packet-list UI, e.g. "SamsungElect") isn't fit for a human-facing tooltip either.

Usage:
    uv run python scripts/generators/gen_fingerprint_vendors.py           # download the current feed
    uv run python scripts/generators/gen_fingerprint_vendors.py manuf     # use a local copy of the file
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional

_MANUF_URL = "https://www.wireshark.org/download/automated/data/manuf"

_OUT = (Path(__file__).resolve().parents[2]
        / "src" / "wifit3" / "wlan" / "fingerprint_vendors.py")

# IEEE only ever allocates at these three widths (MA-L/MA-M/MA-S); manuf's optional "/N" suffix
# names bit-width, but we key by hex nibbles (bits // 4) since every allocation is nibble-aligned.
_ALLOCATION_BITS = (24, 28, 36)

# Stripped iteratively (some names stack two, e.g. "X Technology Co., Ltd."), so each pattern
# only needs to match one trailing suffix at a time. The second line's extras (JSC/OAO/ZAO/OOO
# and "closed/open joint stock company", both common on Russian/CIS registrants; "systems";
# "holding") are ported from Wireshark's make-manuf.py suffix list (GPLv2), which catches more
# than our original set did.
_SUFFIX_RE = re.compile(
    r",?\s*("
    r"incorporated|inc\.?|llc\.?|ltd\.?|co\.,?\s*ltd\.?|corporation|corp\.?|gmbh|"
    r"s\.a\.?|s\.p\.a\.?|b\.v\.?|pty\.?\s*ltd\.?|pvt\.?\s*ltd\.?|ag|company|limited|"
    r"closed joint stock company|open joint stock company|jsc|oao|zao|ooo|systems|holding"
    r")\.?\s*$",
    re.IGNORECASE,
)

# Corrections that don't fit a regex: .title() mangles capitalization around "&" and inside
# established acronyms/brand names since it can't tell a deliberate acronym from ALL-CAPS
# shouting, and a name mixing scripts (Chinese + a parenthetical English translation) isn't
# something _SUFFIX_RE's Latin-only patterns touch at all. "Advanced Micro Devices" -> "AMD"
# and the Chinese entry are ported from Wireshark's make-manuf.py special_case dict (GPLv2);
# the AT&T/Samsung casing fixes are ours, same underlying problem as the AMD one -- confirmed
# Wireshark's own feed doesn't fix AT&T's casing either, so this isn't a duplicated effort.
_SPECIAL_CASE = {
    "At&T": "AT&T",
    "Samsung Electronics": "SAMSUNG Electronics",
    "Advanced Micro Devices": "AMD",
    "杭州德澜科技有限公司（HangZhou Delan Technology Co.,Ltd）": "DelanTech",
}

# Belt-and-suspenders: not known to occur in Wireshark's feed (unlike the raw IEEE registry,
# which labels an unresolved MA-M/MA-S parent block this way), but cheap to guard regardless.
_NOT_A_VENDOR = {"IEEE Registration Authority", "Private"}

# Chinese registrants often lead with the city/province, which _SUFFIX_RE (trailing-only) can't
# touch. Ported from Wireshark's make-manuf.py (GPLv2), non-exhaustive by their own admission;
# strips just the leading word, same as they do, not the whole name.
_SKIP_START = {
    "shengzen", "shenzhen", "beijing", "shanghai", "wuhan", "hangzhou", "guangxi",
    "guangdong", "chengdu",
}


def _clean_name(org: str) -> str:
    name = org.strip().strip('"').strip()
    # Free text: a handful of orgs registered trademark glyphs or an em-dash into their own name
    # (e.g. "Planet Bingo® — 3rd Rock Gaming®"), which the project's own em-dash ban would
    # otherwise trip on generated output.
    name = name.replace("—", "-").replace("–", "-")
    name = name.replace("®", "").replace("™", "")
    words = name.split()
    if len(words) > 1 and words[0].lower() in _SKIP_START:
        name = " ".join(words[1:])
    prev = None
    while prev != name and name:
        prev = name
        name = _SUFFIX_RE.sub("", name).strip()
    if not name:
        return org.strip()
    cleaned = name.title() if name.isupper() else name
    return _SPECIAL_CASE.get(cleaned, cleaned)


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    """One ``<prefix>[/bits]\\t<short name>\\t<long name>`` line -> (hex-nibble prefix, raw long
    name), or ``None`` for a comment/blank/unrecognized-width line; ``bits`` defaults to 24."""
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        return None
    fields = [f for f in line.split("\t") if f.strip()]
    if len(fields) < 2:
        return None
    prefix_field = fields[0].strip()
    long_name = fields[2].strip() if len(fields) >= 3 else fields[1].strip()
    mac_part, _, bits_str = prefix_field.partition("/")
    bits = int(bits_str) if bits_str else 24
    if bits not in _ALLOCATION_BITS:
        return None
    nibbles = bits // 4
    hex_digits = mac_part.replace(":", "").replace("-", "").upper()
    prefix = hex_digits[:nibbles]
    if len(prefix) != nibbles or not all(c in "0123456789ABCDEF" for c in prefix):
        return None
    return prefix, long_name


def _load(source: str | None) -> str:
    if source:
        return Path(source).read_text(encoding="utf-8", errors="replace")
    print(f"Downloading {_MANUF_URL} ...")
    req = urllib.request.Request(_MANUF_URL, headers={"User-Agent": "wifit3-fingerprint-vendor-gen"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (fixed wireshark.org https host)
        return r.read().decode("utf-8", "replace")


def main(source: str | None = None) -> None:
    mapping: dict[str, str] = {}
    for line in _load(source).splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        prefix, raw_name = parsed
        name = _clean_name(raw_name)
        if name in _NOT_A_VENDOR:
            continue
        mapping[prefix] = name

    header = (
        '"""OUI -> cleaned vendor name, for client fingerprinting\'s low-confidence tier\n'
        "(wlan/fingerprint.py): every registered vendor, since naming who made a device --\n"
        "without claiming which kind -- is the entire point of that tier.\n\n"
        "Keys vary in length: most are 6 hex chars (a plain 24-bit OUI), some are 7 or 9 (IEEE's\n"
        "finer-grained 28-/36-bit sub-allocations, where a single 24-bit block is actually split\n"
        "between several organizations). fingerprint.py tries the longest prefix first.\n\n"
        "GENERATED by scripts/generators/gen_fingerprint_vendors.py from Wireshark's weekly\n"
        "manuf feed (wireshark.org/download/automated/data/manuf, GPLv2). Do NOT edit by hand;\n"
        'rerun the script to refresh.\n"""\n\n'
    )
    body = "VENDOR_BY_OUI = {\n"
    body += "".join(f'    "{o}": {mapping[o]!r},\n' for o in sorted(mapping))
    body += "}\n"
    _OUT.write_text(header + body, encoding="utf-8")
    print(f"Wrote {len(mapping)} OUIs -> {_OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
