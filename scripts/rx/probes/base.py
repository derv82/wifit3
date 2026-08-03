"""Probe protocol + shared helpers used by every diagnostic probe.

The soak harness orchestrates probes in three phases:

1. **attach**: every enabled probe gets a chance to register rx
   callbacks or stash setup state. Runs before any active probe does
   work. This is what passive probes (e.g. parse_quality) hang their
   measurement on.

2. **run**: active probes do their work (channel dwell, hopping,
   etc.) and return a result. Passive probes return ``None`` here.

3. **finalize**: passive probes return their accumulated stats.
   Active probes return whatever ``run`` already gave them.

Probes that fit one mold leave the other methods as no-ops; both kinds
co-exist in the registry without orchestrator branching.
"""
from __future__ import annotations

import argparse
import statistics
from typing import Any, Protocol, runtime_checkable


def classify_band(channel: int) -> str:
    if 1 <= channel <= 14:
        return "2.4"
    if channel >= 36:
        return "5"
    return "?"


class FrameCounter:
    """Raw RX frame counter, hookable via ``iface.register_rx_callback``.

    The interface fires every registered callback on every frame, so
    multiple counters can coexist (e.g. one per probe + parse_quality's
    parser). Each one observes independently.
    """

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, pkt) -> None:
        self.count += 1


def snapshot_active(array, since: float):
    """Return (active_total, active_24, active_5, channels_seen)."""
    aps = [ap for ap in list(array.access_points.values()) if ap.last_seen >= since]
    a24 = sum(1 for ap in aps if classify_band(ap.channel) == "2.4")
    a5 = sum(1 for ap in aps if classify_band(ap.channel) == "5")
    chans_seen = {ap.channel for ap in aps}
    return len(aps), a24, a5, chans_seen


def median_int(values) -> int:
    """statistics.median on an iterable of numbers, cast to int. Empty
    iterables return 0; callers should guard if that's wrong for them."""
    values = list(values)
    if not values:
        return 0
    return int(statistics.median(values))


@runtime_checkable
class Probe(Protocol):
    """Diagnostic probe contract.

    ``name`` is the CLI slug, used to build ``--skip-<name>`` and to
    label the section in the rendered report. Keep it short, lowercase,
    no spaces.
    """

    name: str

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        """Register probe-specific CLI flags. soak.py adds the
        ``--skip-<name>`` flag itself; probes only add their own."""
        ...

    def is_enabled(self, args: argparse.Namespace) -> bool:
        """Return True if this probe should run. Default: enabled
        unless ``--skip-<name>`` is set."""
        ...

    def apply_multiplier(self, args: argparse.Namespace, mult: float) -> None:
        """Scale any duration-style args this probe owns by ``mult``.
        Called once after argparse but before any probe runs. No-op
        for probes with no duration."""
        ...

    def attach(self, iface) -> None:
        """Hook into the interface (rx callbacks, etc.) before any
        active probe runs. Default: no-op. Passive probes do their
        measurement-side wiring here."""
        ...

    async def run(self, iface, args: argparse.Namespace) -> Any:
        """Active work. Returns the result the report renderer will
        consume. Passive probes return None here and rely on
        ``finalize`` to surface their stats."""
        ...

    def finalize(self) -> Any:
        """Return the final result after every active probe has
        completed. Active probes typically leave this returning
        ``None``. The renderer falls back to ``run``'s return value."""
        ...

    def verdict_lines(self, result: Any, args: argparse.Namespace) -> list[str]:
        """One-liners that go in the report's 'Quick verdict' bullet
        list. Empty list if this probe has nothing terse to say."""
        ...

    def report_lines(self, result: Any, args: argparse.Namespace) -> list[str]:
        """Markdown lines for this probe's detailed section. The
        renderer concatenates each probe's lines in registry order."""
        ...

    def csv_section(self, writer, result: Any) -> None:
        """Append this probe's rows to the shared CSV. Each probe
        owns its own header row. Empty section is fine."""
        ...
