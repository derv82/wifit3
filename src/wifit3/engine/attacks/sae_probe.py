"""SAE Group Probe — enumerate which SAE finite cyclic groups a WPA3 AP accepts.

For each candidate group we inject an Authentication frame with algo=3 (SAE),
transaction=1 (Commit), status=0, and a dummy scalar + element. The AP doesn't
need to validate the math — it parses the group ID first and rejects with status
77 (UNSUPPORTED_FINITE_CYCLIC_GROUP) if it doesn't support that group, or accepts
with status 0 / 76 (ANTI_CLOGGING_TOKEN_REQ) if it does. Either response is a
fingerprint of the AP's SAE config.

Dragonblood-relevant groups: 22, 23, 24 (legacy FFC groups) — APs that still
support these alongside Group 19 are vulnerable to the side-channel attacks in
CVE-2019-9494 / 9495.
"""

import asyncio
import logging
import os
import struct
import time
from typing import Dict, Optional, Tuple

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


# 802.11 status codes relevant to SAE Commit responses.
STATUS_SUCCESS = 0
STATUS_UNSPECIFIED_FAILURE = 1  # AP parsed past group-check, rejected the (dummy) scalar/element math
STATUS_ANTI_CLOGGING_TOKEN_REQ = 76
STATUS_UNSUPPORTED_GROUP = 77


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC (LAA bit set, multicast bit clear)."""
    return bytes([0x02]) + os.urandom(5)


class SAEGroupProbeAttack:
    """Probe an AP for which SAE finite cyclic groups it accepts."""

    def __init__(self, iface, target: AccessPoint, source_mac: Optional[bytes] = None):
        self.iface = iface
        self.target = target
        # Tuple of (label, detail) per group so the UI can colour by risk.
        self.results: Dict[int, Tuple[str, str]] = {}
        self.source_mac = source_mac or _random_client_mac()
        self.iface.register_forged_mac(self.source_mac)

    def _craft_sae_commit(self, bssid_mac: bytes, source_mac: bytes, group_id: int) -> bytes:
        # MAC header: Mgmt(00) + Auth(1011) subtype → FC byte0=0xB0, byte1=0x00.
        # Addr1=BSSID(dest), Addr2=us(source), Addr3=BSSID. Seq filled by HW.
        mac_header = (
            b"\xb0\x00"
            + b"\x00\x00"
            + bssid_mac
            + source_mac
            + bssid_mac
            + b"\x00\x00"
        )

        # Auth body: algo=3 (SAE), transaction=1 (Commit), status=0, group_id (2B LE),
        # then dummy scalar (32B) + element (32B). Math is irrelevant — the AP rejects
        # at group-check before validating the scalar/element.
        auth_algo = b"\x03\x00"
        transaction = b"\x01\x00"
        status = b"\x00\x00"
        group = struct.pack("<H", group_id)
        dummy_scalar = b"\x11" * 32
        dummy_element = b"\x22" * 32

        return mac_header + auth_algo + transaction + status + group + dummy_scalar + dummy_element

    @staticmethod
    def _classify(status_code: Optional[int]) -> Tuple[str, str]:
        """Return (short_label, detail) for a given SAE Commit response status."""
        if status_code is None:
            return ("Timeout", "No response (frame dropped or AP ignored)")
        if status_code == STATUS_SUCCESS:
            return ("Supported", "Status 0 — group accepted, AP sent Commit reply")
        if status_code == STATUS_ANTI_CLOGGING_TOKEN_REQ:
            return ("Supported", "Status 76 — anti-clogging token required (group OK)")
        if status_code == STATUS_UNSPECIFIED_FAILURE:
            # AP parsed past the group-check and only then failed validating our
            # dummy scalar/element. Strong evidence the group itself is accepted.
            return ("Supported", "Status 1 — AP rejected our dummy Commit math (group OK)")
        if status_code == STATUS_UNSUPPORTED_GROUP:
            return ("Rejected", "Status 77 — group not supported by AP")
        return ("Unknown", f"Status {status_code}")

    async def run(self, groups_to_test=(19, 20, 21, 22, 23, 24), timeout: float = 1.0):
        """Probe each group sequentially. Skips groups already cached on the
        AccessPoint (``target.sae_groups``) and writes new definitive results
        back to that cache. Returns ``{group_id: (label, detail)}`` covering
        BOTH cached and freshly-probed groups, so the UI can render the full
        picture on every run."""
        if not self.target.wpa3:
            logger.warning(f"Target {self.target.bssid} is not marked as WPA3.")

        # Defensive channel tune — Focus view should have done this, but if the
        # hopper resumed for any reason the probe would silently miss responses.
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        bssid_bytes = bytes(int(x, 16) for x in self.target.bssid.split(":"))
        cache = self.target.sae_groups

        # Seed results from cache so the returned dict is cumulative.
        for group, verdict in cache.items():
            if verdict == "supported":
                self.results[group] = ("Supported", "Cached from prior probe")
            elif verdict == "rejected":
                self.results[group] = ("Rejected", "Cached from prior probe")

        for group in groups_to_test:
            if cache.get(group) in ("supported", "rejected"):
                logger.info(f"[SAE] Group {group} cached as {cache[group]} — skipping.")
                continue

            logger.info(f"[SAE] Probing Group {group} on {self.target.bssid}")
            frame = self._craft_sae_commit(bssid_bytes, self.source_mac, group)

            response_status: Optional[int] = None

            def auth_callback(frame_bytes: bytes, rssi: int, ts: float):
                nonlocal response_status
                # Need MAC header (24B) + algo (2) + seq (2) + status (2).
                if len(frame_bytes) < 30:
                    return
                # FC byte0: type=Mgmt(00), subtype=Auth(1011) → 0xB0; mask out version.
                if (frame_bytes[0] & 0xFC) != 0xB0:
                    return
                addr1 = frame_bytes[4:10]
                addr2 = frame_bytes[10:16]
                # AP → us: Addr1=our forged MAC, Addr2=AP BSSID.
                if addr1 != self.source_mac or addr2 != bssid_bytes:
                    return
                body = frame_bytes[24:]
                algo = struct.unpack("<H", body[0:2])[0]
                seq = struct.unpack("<H", body[2:4])[0]
                if algo == 3 and seq == 1:
                    response_status = struct.unpack("<H", body[4:6])[0]

            self.iface.register_rx_callback(auth_callback)
            try:
                await self.iface.send_raw(frame, use_no_ack=True)

                deadline = time.time() + timeout
                while time.time() < deadline and response_status is None:
                    await asyncio.sleep(0.05)
            finally:
                self.iface.unregister_rx_callback(auth_callback)

            label, detail = self._classify(response_status)
            self.results[group] = (label, detail)
            # Only cache definitive verdicts — timeouts / unknowns get re-probed.
            if label == "Supported":
                cache[group] = "supported"
            elif label == "Rejected":
                cache[group] = "rejected"
            logger.info(f"[SAE] Group {group}: {label} — {detail}")

        return self.results
