"""Parse-quality probe (passive).

Hooks ``iface.register_rx_callback`` and inspects the parsed output of
every frame the other probes are already producing. Two signals:

1. **OUI sanity** — BSSID's first three bytes shouldn't be multicast,
   all-zero, all-FF, or all-same-byte. Catches bit-flip BSSIDs from
   split-MPDU bugs (the descriptor decoder cut the wrong byte
   boundary) and from RX-DMA underruns. Cheap, deterministic.

2. **Beacon channel consistency** — a beacon's DS Param IE primary
   channel should match the chip's currently-tuned channel. Mismatches
   indicate either a loose tune (the RF is hearing an adjacent
   channel) or a misordered RX queue (the descriptor's channel field
   doesn't match the frame's actual capture moment).

This probe is **passive**: ``attach`` registers the rx callback,
``run`` is a no-op, ``finalize`` returns the accumulated stats. It
piggybacks on whatever active probes ran — so its statistical signal
is best when the long-run probe also ran.

## Out of scope for Phase 1

* **Raw parse-failure detection** — the iface only fires rx callbacks
  for frames the driver's descriptor decoder + WlanFrameParser already
  agreed are valid 802.11. To measure parse failure as a *rate* we'd
  need a pre-parse tap in every driver's RX loop. Tracked as Phase 1.5.

* **FCS validation** — per-driver (some chips strip the FCS before
  delivery, some don't). Needs a per-driver ``STRIPS_FCS: bool``
  capability. Tracked as Phase 1.5.
"""
from __future__ import annotations

import argparse
from typing import Any

from .base import Probe

# sys.path is set up by sweep.py before this module is loaded
# (sweep prepends src/ so wifit3.* is importable).
from wifit3.dot11.parser import WlanFrameParser  # noqa: E402


def _bssid_sane(bssid: str | None) -> bool:
    """True if BSSID looks like a plausible AP MAC.

    Rejects: None, malformed, multicast (LSB of first byte set),
    all-zero, all-FF, all-same-byte. These cover the bit-flip /
    split-MPDU classes that produce structurally-valid 802.11 frames
    whose addresses are garbage.
    """
    if not bssid:
        return False
    parts = bssid.split(":")
    if len(parts) != 6:
        return False
    try:
        byts = [int(p, 16) for p in parts]
    except ValueError:
        return False
    if byts[0] & 0x01:  # multicast / broadcast
        return False
    if all(b == 0x00 for b in byts):
        return False
    if all(b == 0xFF for b in byts):
        return False
    if all(b == byts[0] for b in byts):
        return False
    return True


class ParseQualityProbe(Probe):
    name = "parse-quality"

    def __init__(self) -> None:
        self._iface = None
        self._total = 0
        self._oui_sane = 0
        self._oui_garbage = 0
        self._beacons = 0
        self._beacons_with_ds = 0
        self._beacon_ch_match = 0
        self._beacon_ch_mismatch = 0
        self._examples: list[str] = []  # first few garbage BSSIDs seen

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        # No own args yet — runs whenever active probes deliver frames.
        pass

    def is_enabled(self, args: argparse.Namespace) -> bool:
        return not args.skip_parse_quality

    def apply_multiplier(self, args: argparse.Namespace, mult: float) -> None:
        pass

    def attach(self, iface) -> None:
        self._iface = iface
        iface.register_rx_callback(self._on_raw)

    async def run(self, iface, args: argparse.Namespace) -> Any:
        # Passive — all the work happens in the rx callback.
        return None

    def finalize(self) -> Any:
        return dict(
            total=self._total,
            oui_sane=self._oui_sane,
            oui_garbage=self._oui_garbage,
            beacons=self._beacons,
            beacons_with_ds=self._beacons_with_ds,
            beacon_ch_match=self._beacon_ch_match,
            beacon_ch_mismatch=self._beacon_ch_mismatch,
            garbage_examples=list(self._examples),
        )

    def _on_raw(self, raw: bytes, rssi: int, ts: float) -> None:
        # Re-parse to inspect. The driver already parsed this same
        # frame upstream, so we always expect a dict back here — but
        # we guard anyway so a parser hiccup doesn't crash diag.
        try:
            parsed = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:
            return
        if parsed is None:
            return

        self._total += 1
        bssid = parsed.bssid
        if _bssid_sane(bssid):
            self._oui_sane += 1
        else:
            self._oui_garbage += 1
            if len(self._examples) < 5 and bssid:
                self._examples.append(bssid)

        if parsed.type == "beacon":
            self._beacons += 1
            beacon_ch = parsed.channel
            if beacon_ch is not None:
                self._beacons_with_ds += 1
                # iface.current_channel may legitimately have moved on
                # between RX capture and our callback (hopping). The
                # snapshot at this instant is the best we have; cross-
                # band mismatches are still the strong signal.
                tuned = self._iface.current_channel if self._iface else None
                if tuned is not None and beacon_ch == tuned:
                    self._beacon_ch_match += 1
                else:
                    self._beacon_ch_mismatch += 1

    def verdict_lines(self, result, args) -> list[str]:
        if not result or result["total"] == 0:
            return ["- Parse-quality probe: no frames observed."]
        total = result["total"]
        garbage = result["oui_garbage"]
        garbage_pct = 100.0 * garbage / total
        lines: list[str] = []
        flag = "WARN " if garbage_pct > 1.0 else "OK   "
        lines.append(
            f"- {flag} BSSID OUI sanity: {garbage}/{total} garbage "
            f"({garbage_pct:.2f}%)."
        )
        beacons = result["beacons"]
        if beacons:
            mism = result["beacon_ch_mismatch"]
            mism_pct = 100.0 * mism / beacons
            # Mismatch is noisy when the hopper is moving fast — only
            # WARN above 20%. Lower bound surfaces the signal without
            # crying wolf on every hop.
            flag = "WARN " if mism_pct > 20.0 else "OK   "
            lines.append(
                f"- {flag} Beacon channel match (DS IE vs current tune): "
                f"{result['beacon_ch_match']}/{beacons} match, "
                f"{mism} mismatch ({mism_pct:.1f}%)."
            )
        return lines

    def report_lines(self, result, args) -> list[str]:
        if not result or result["total"] == 0:
            return []
        lines: list[str] = [
            "## Section 3 - Parse quality",
            "",
            f"- Frames inspected: {result['total']}",
            f"- BSSID OUI sane: {result['oui_sane']}",
            f"- BSSID OUI garbage: {result['oui_garbage']}",
        ]
        if result["garbage_examples"]:
            lines.append(
                f"- First garbage BSSIDs: {', '.join(result['garbage_examples'])}"
            )
        if result["beacons"]:
            lines.append(f"- Beacons: {result['beacons']}")
            lines.append(
                f"- Beacons with DS Param IE: {result['beacons_with_ds']}"
            )
            lines.append(
                f"- Beacon channel match: {result['beacon_ch_match']}"
            )
            lines.append(
                f"- Beacon channel mismatch: {result['beacon_ch_mismatch']}"
            )
        lines.append("")
        lines.append(
            "_Note: this probe sees only frames the driver+parser already "
            "accepted. Pre-parse failure rate (Phase 1.5) needs a per-driver "
            "raw tap and is not measured here._"
        )
        lines.append("")
        return lines

    def csv_section(self, w, result) -> None:
        if not result:
            return
        w.writerow(["# parse-quality"])
        w.writerow([
            "total", "oui_sane", "oui_garbage",
            "beacons", "beacons_with_ds",
            "beacon_ch_match", "beacon_ch_mismatch",
        ])
        w.writerow([
            result["total"], result["oui_sane"], result["oui_garbage"],
            result["beacons"], result["beacons_with_ds"],
            result["beacon_ch_match"], result["beacon_ch_mismatch"],
        ])
        if result["garbage_examples"]:
            w.writerow([])
            w.writerow(["# parse-quality.garbage_bssid_examples"])
            for ex in result["garbage_examples"]:
                w.writerow([ex])
        w.writerow([])
