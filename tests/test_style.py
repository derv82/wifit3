"""Style guards for the maintained core (chips/ excluded: ported code cites kernel C)."""
from __future__ import annotations

from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "src" / "wifit3"
_EMDASH = "—"


def _core_files() -> list[Path]:
    return [p for p in _CORE.rglob("*.py") if "chips" not in p.parts]


def test_no_emdash_in_core():
    hits = [f"{p.relative_to(_CORE)}:{i}"
            for p in _core_files()
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if _EMDASH in line]
    assert not hits, "em-dash (U+2014) is banned; found:\n" + "\n".join(hits)
