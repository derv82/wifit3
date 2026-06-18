"""Focus view v2 — the spatial router-admin redesign (landscape).

The default Focus screen; the legacy v1 panel grid is kept behind
``WIFIT3_FOCUS_V1=1`` during the soak (see ``ui/app.py``). The shared brains live
in ``ui/focus_model.py`` (outside this package, imported by both views)."""
from .screen import FocusViewV2

__all__ = ["FocusViewV2"]
