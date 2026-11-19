#!/usr/bin/env python3
"""Reproducibly extract RTL8192EU-specific register tables from rtl8xxxu_8192e.c.

This script handles the `EXT_PA_8192EU` branch explicitly: the default
(no external PA) path is selected when building the ordinary table. This
is not a general-purpose C preprocessor. Review any source format changes.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re

TABLES = (
    "rtl8192e_mac_init_table",
    "rtl8192eu_phy_init_table",
    "rtl8xxx_agc_8192eu_std_table",
    "rtl8xxx_agc_8192eu_highpa_table",
    "rtl8192eu_radioa_init_table",
    "rtl8192eu_radiob_init_table",
)


def extract(source: str, table: str, *, external_pa: bool = False) -> tuple[tuple[int, int], ...]:
    pattern = rf"\b{re.escape(table)}\[\]\s*=\s*\{{(.*?)\n\}};"
    match = re.search(pattern, source, re.S)
    if match is None:
        raise ValueError(f"table missing: {table}")
    active = True
    stack = []
    result = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("#ifdef EXT_PA_8192EU"):
            stack.append(active)
            active = active and external_pa
            continue
        if stripped == "#else":
            if not stack:
                raise ValueError(f"unexpected #else in {table}")
            active = stack[-1] and not external_pa
            continue
        if stripped == "#endif":
            if not stack:
                raise ValueError(f"unexpected #endif in {table}")
            active = stack.pop()
            continue
        if stripped.startswith('#'):
            raise ValueError(f"unsupported preprocessor directive in {table}: {stripped}")
        if active:
            for address, value in re.findall(r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}", line):
                result.append((int(address, 16), int(value, 16)))
    if stack:
        raise ValueError(f"unterminated condition in {table}")
    if not result:
        raise ValueError(f"empty table: {table}")
    return tuple(result)


def render(source: str, source_sha: str) -> str:
    lines = [
        '# SPDX-License-Identifier: GPL-2.0-only',
        '"""RTL8192EU source-derived tables; generated, do not edit.',
        '',
        'Source: rtl8xxxu_8192e.c (user-supplied Microsoft WSL 6.6 source).',
        f'Source SHA-256: {source_sha}',
        'Original source copyright: Jes Sorensen and Realtek Corporation.',
        '"""',
        '',
    ]
    for name in TABLES:
        data = extract(source, name)
        lines.append(f'{name.upper()} = (')
        lines.extend(f'    (0x{a:04x}, 0x{v:08x}),' for a,v in data)
        lines.append(')')
        lines.append('')
    lines.append('RTL8192EU_PHY_INIT_TABLE_EXT_PA = (')
    lines.extend(f'    (0x{a:04x}, 0x{v:08x}),' for a,v in extract(source, 'rtl8192eu_phy_init_table', external_pa=True))
    lines.append(')')
    lines.append('')
    return '\n'.join(lines).replace('\\n','\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path, help='path to rtl8xxxu_8192e.c')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    import hashlib
    body = args.source.read_text()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(body, hashlib.sha256(args.source.read_bytes()).hexdigest()))

if __name__ == '__main__':
    main()
