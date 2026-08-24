"""Style guards for the maintained core (chips/ excluded: ported code cites kernel C)."""
from __future__ import annotations

import ast
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent / "src" / "wifit3"
_EMDASH = "—"
_MAX_DOC_LINES = 2                 # a 3+ line function/class docstring is the banned smell
_DOC_BASELINE = 183                # known fat docstrings today; ratchet DOWN to 0, never up
_DECL = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _core_files() -> list[Path]:
    return [p for p in _CORE.rglob("*.py") if "chips" not in p.parts]


def test_no_emdash_in_core():
    hits = [f"{p.relative_to(_CORE)}:{i}"
            for p in _core_files()
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if _EMDASH in line]
    assert not hits, "em-dash (U+2014) is banned; found:\n" + "\n".join(hits)


def _fat_docstrings(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, _DECL):
            doc = ast.get_docstring(node)
            if doc and len(doc.splitlines()) > _MAX_DOC_LINES:
                yield f"{path.relative_to(_CORE)}:{node.lineno} {node.name}"


def test_core_docstrings_ratchet_down():
    fat = sorted(d for p in _core_files() for d in _fat_docstrings(p))
    # Ratchet: cannot exceed today's count, so bloat can only shrink. Lower the
    # baseline whenever it passes with room, until it reaches 0.
    assert len(fat) <= _DOC_BASELINE, (
        f"{len(fat)} core docstrings exceed {_MAX_DOC_LINES} lines "
        f"(baseline {_DOC_BASELINE}); tighten new ones, do not add:\n" + "\n".join(fat))
