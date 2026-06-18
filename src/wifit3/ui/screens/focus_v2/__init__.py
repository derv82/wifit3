"""Focus view v2 — the spatial router-admin redesign (landscape, shell stage).

A throwaway-able package selected behind ``WIFIT3_FOCUS_V2=1``; the abandon path
is ``rm -rf focus_v2/`` + the flag branch in ``ui/app.py``. The shared brains
live in ``ui/focus_model.py`` (outside this package, imported by both views)."""
from .screen import FocusViewV2

__all__ = ["FocusViewV2"]
