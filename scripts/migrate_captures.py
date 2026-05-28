"""One-off captures/ migrator: rename old artifacts to the engine.save scheme.

Renames performed:

  - <base>_wepkey.txt          → <base>_wep_key.txt           (rename, body unchanged)
  - <base>.wps  method=WPS-PBC → <base>_wps_pbc.txt           (strip method: line)
  - <base>.wps  method=WPS-PIN → <base>_wps_pin.txt           (strip method: line)
  - <base>.hc22000             → <base>_handshake.hc22000 and/or _pmkid.hc22000
                                 (split by WPA*02 vs WPA*01; both if mixed)
  - <base>.pcap                → <base>_<kind>.pcap, kind from sibling .hc22000;
                                 copied to both names if the sibling splits.

Dry-run by default. Pass --apply to act. Idempotent: post-migration names
don't match any input pattern, so re-runs no-op.

Usage:

    uv run python scripts/migrate_captures.py captures/            # dry-run
    uv run python scripts/migrate_captures.py captures/ --apply    # act
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

_WEPKEY_RE = re.compile(r"^(?P<base>.+)_wepkey\.txt$")
_HC22000_RE = re.compile(r"^(?P<base>.+)\.hc22000$")
_PCAP_RE = re.compile(r"^(?P<base>.+)\.pcap$")
_WPS_RE = re.compile(r"^(?P<base>.+)\.wps$")
_METHOD_RE = re.compile(r"^method:\s*(WPS-PBC|WPS-PIN)\s*$", re.MULTILINE)

# Names that already match the engine.save scheme — skip so re-runs are no-ops.
_MIGRATED_SUFFIXES = (
    "_handshake.hc22000", "_pmkid.hc22000",
    "_handshake.pcap", "_pmkid.pcap",
    "_wep_key.txt", "_wps_pin.txt", "_wps_pbc.txt",
)


@dataclass
class Plan:
    renames: list[tuple[Path, Path]] = field(default_factory=list)
    rewrites: list[tuple[Path, Path, str]] = field(default_factory=list)
    copies: list[tuple[Path, Path]] = field(default_factory=list)
    deletes: list[Path] = field(default_factory=list)
    skips: list[tuple[Path, str]] = field(default_factory=list)


def _classify_hc22000(text: str) -> list[str]:
    has_hs = any(line.startswith("WPA*02*") for line in text.splitlines())
    has_pmkid = any(line.startswith("WPA*01*") for line in text.splitlines())
    out: list[str] = []
    if has_hs:
        out.append("handshake")
    if has_pmkid:
        out.append("pmkid")
    return out


def _strip_method(text: str) -> str:
    kept = [ln for ln in text.splitlines() if not ln.lower().startswith("method:")]
    return "\n".join(kept).rstrip() + "\n"


def _wps_method(text: str) -> str | None:
    m = _METHOD_RE.search(text)
    return m.group(1) if m else None


def build_plan(directory: Path) -> Plan:
    plan = Plan()
    files = sorted(p for p in directory.iterdir()
                   if p.is_file() and not p.name.endswith(_MIGRATED_SUFFIXES))

    # Pass 1: classify every .hc22000 (pcap migration needs the sibling verdict).
    hc_kinds: dict[str, list[str]] = {}
    for path in files:
        m = _HC22000_RE.match(path.name)
        if not m:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            plan.skips.append((path, "unreadable"))
            continue
        kinds = _classify_hc22000(text)
        if not kinds:
            plan.skips.append((path, "no WPA*02/*01 lines"))
            continue
        hc_kinds[m.group("base")] = kinds

    # Pass 2: build operations.
    for path in files:
        name = path.name

        if (m := _WEPKEY_RE.match(name)):
            dst = path.with_name(f"{m.group('base')}_wep_key.txt")
            if dst.exists():
                plan.skips.append((path, f"target exists: {dst.name}"))
            else:
                plan.renames.append((path, dst))
            continue

        if (m := _WPS_RE.match(name)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                plan.skips.append((path, "unreadable"))
                continue
            method = _wps_method(text)
            if method is None:
                plan.skips.append((path, "no 'method:' line"))
                continue
            suffix = "wps_pbc" if method == "WPS-PBC" else "wps_pin"
            dst = path.with_name(f"{m.group('base')}_{suffix}.txt")
            if dst.exists():
                plan.skips.append((path, f"target exists: {dst.name}"))
            else:
                plan.rewrites.append((path, dst, _strip_method(text)))
                plan.deletes.append(path)
            continue

        if (m := _HC22000_RE.match(name)):
            base = m.group("base")
            kinds = hc_kinds.get(base, [])
            if not kinds:
                continue  # already noted in pass 1
            dsts = [path.with_name(f"{base}_{k}.hc22000") for k in kinds]
            existing = [d for d in dsts if d.exists()]
            if existing:
                plan.skips.append((path, f"target(s) exist: {', '.join(d.name for d in existing)}"))
                continue
            if len(kinds) == 1:
                plan.renames.append((path, dsts[0]))
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                for k, dst in zip(kinds, dsts):
                    prefix = "WPA*02*" if k == "handshake" else "WPA*01*"
                    body = "\n".join(ln for ln in text.splitlines() if ln.startswith(prefix)) + "\n"
                    plan.rewrites.append((path, dst, body))
                plan.deletes.append(path)
            continue

        if (m := _PCAP_RE.match(name)):
            base = m.group("base")
            kinds = hc_kinds.get(base, [])
            if not kinds:
                plan.skips.append((path, "no .hc22000 sibling — cannot determine kind"))
                continue
            dsts = [path.with_name(f"{base}_{k}.pcap") for k in kinds]
            existing = [d for d in dsts if d.exists()]
            if existing:
                plan.skips.append((path, f"target(s) exist: {', '.join(d.name for d in existing)}"))
                continue
            if len(kinds) == 1:
                plan.renames.append((path, dsts[0]))
            else:
                for d in dsts:
                    plan.copies.append((path, d))
                plan.deletes.append(path)
            continue

    return plan


def print_plan(plan: Plan) -> None:
    if plan.renames:
        print("Renames:")
        for s, d in plan.renames:
            print(f"  {s.name}  →  {d.name}")
    if plan.rewrites:
        print("Rewrites (write new + delete source after):")
        for s, d, _ in plan.rewrites:
            print(f"  {s.name}  →  {d.name}")
    if plan.copies:
        print("Copies (pcap split):")
        for s, d in plan.copies:
            print(f"  {s.name}  →  {d.name}")
    if plan.deletes:
        print("Deletes (sources to remove after split/rewrite):")
        for p in sorted(set(plan.deletes)):
            print(f"  {p.name}")
    if plan.skips:
        print("Skipped:")
        for p, reason in plan.skips:
            print(f"  {p.name}  —  {reason}")
    n_ops = len(plan.renames) + len(plan.rewrites) + len(plan.copies)
    print(f"\nTotal: {n_ops} write(s), {len(set(plan.deletes))} delete(s), {len(plan.skips)} skip(s).")


def apply_plan(plan: Plan) -> None:
    for s, d in plan.renames:
        s.rename(d)
    for _s, d, body in plan.rewrites:
        d.write_text(body, encoding="utf-8")
    for s, d in plan.copies:
        shutil.copy2(s, d)
    for p in sorted(set(plan.deletes)):
        if p.exists():
            p.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="captures/ directory to migrate")
    parser.add_argument("--apply", action="store_true",
                        help="actually move/rewrite/delete (otherwise dry-run)")
    args = parser.parse_args(argv)

    d: Path = args.directory
    if not d.is_dir():
        print(f"error: not a directory: {d}", file=sys.stderr)
        return 2

    plan = build_plan(d)
    print_plan(plan)
    if not args.apply:
        print("\n(dry-run — re-run with --apply to act)")
        return 0
    apply_plan(plan)
    print("\napplied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
