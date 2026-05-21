"""Diagnostic probes.

Ordered registry — sweep.py iterates this list to register CLI flags
and to run the active probes. Order is the run order; passive probes
attach first regardless.
"""
from __future__ import annotations

from .base import Probe
from .baseline import BaselineProbe
from .longrun import LongRunProbe
from .parse_quality import ParseQualityProbe

ALL_PROBES: list[Probe] = [
    BaselineProbe(),
    LongRunProbe(),
    ParseQualityProbe(),
]

__all__ = ["Probe", "BaselineProbe", "LongRunProbe", "ParseQualityProbe", "ALL_PROBES"]
