"""Shared Focus view-model — the campaign-value picture, decoupled from layout.

Both the v1 ``FocusView`` and the v2 ``FocusViewV2`` paint from a
``FocusSnapshot``: a per-tick, render-ready description of the target derived
from ``(ap, iface, campaigns)``. v1 still computes those derivations inline
today; extracting them into here is step 1 of the migration (see
``planning/FOCUS-REDESIGN.md``). For now this defines the snapshot shape plus a
``fake_snapshot()`` the v2 shell paints, so the layout is provable before any
real campaign wiring exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FlowRow:
    """One row of the packet-flow channel."""
    key: str                       # beacon / data / wep_iv / eapol / inject / deauth
    label: str                     # <= 6-char gutter label
    color: str                     # Rich colour name
    peak: int                      # nominal scale (drives the fake generator)
    as_rate: bool = True           # True -> "N/s", False -> a recent count


@dataclass
class ClientRow:
    bssid: str
    power: int
    packets: int


@dataclass
class FocusSnapshot:
    status: list[str]              # up to 3 headline lines (the focal point)
    power_dbm: int
    signal: float                  # 0..1, drives the signal bar
    card_chipset: str
    card_bssid: str | None         # the card's own MAC, when the driver exposes it
    card_dynamic: str              # "● replaying" etc; "" when idle
    buttons: list[str]             # encryption-conditional attack-button labels
    ap_essid: str
    ap_bssid: str
    ap_channel: int
    ap_encryption: str             # e.g. "WPA2/CCMP"
    flow: list[FlowRow]
    clients: list[ClientRow]
    log_lines: list[str] = field(default_factory=list)


def fake_snapshot() -> FocusSnapshot:
    """The WPA2-downgrade scenario from the redesign mockup — every region
    populated, so the shell exercises the full layout with no live data."""
    return FocusSnapshot(
        status=[
            "● WPA Downgrade active",
            "deauthing 2 clients · waiting for M1·M2",
            "handshake:  M1 ✓   M2 —",
        ],
        power_dbm=-71,
        signal=0.62,
        card_chipset="rtl8187l",
        card_bssid="00:c0:ca:11:22:33",
        card_dynamic="● deauthing",
        buttons=["Extract PMKID", "WPA Downgrade", "WPS Brute Force"],
        ap_essid="NETGEAR91",
        ap_bssid="a8:fc:b7:0e:1d:42",
        ap_channel=6,
        ap_encryption="WPA2/CCMP",
        flow=[
            FlowRow("beacon", "beacon", "cyan", 10),
            FlowRow("data", "data", "blue", 240),
            FlowRow("eapol", "eapol", "green", 4, as_rate=False),
            FlowRow("inject", "inject", "orange1", 30),
            FlowRow("deauth", "deauth", "red", 12),
        ],
        clients=[
            ClientRow("fa:11:22:33:44:aa", -79, 10),
            ClientRow("04:2e:c1:51:43:b8", -80, 134),
            ClientRow("9c:b6:d0:1a:2b:3c", -67, 512),
            ClientRow("3a:f1:08:77:aa:01", -83, 22),
            ClientRow("de:ad:be:ef:00:42", -75, 88),
        ],
        log_lines=[
            "19:41:58  Listening on ch 6",
            "19:42:00  Beacon ◂ target AP",
            "19:42:01  Target locked.",
            "19:42:02  2 clients seen",
            "19:42:03  Deauth ▸ ff:ff:ff…",
            "19:42:03  Deauth ▸ fa:11:…:aa",
            "19:42:04  M1 captured (ANonce)",
            "19:42:05  Waiting for M2…",
            "19:42:06  Deauth ▸ 04:2e:…:b8",
            "19:42:07  Client reassoc",
            "19:42:08  M1 captured (ANonce)",
            "19:42:09  Waiting for M2…",
        ],
    )
