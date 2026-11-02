"""Broadcom wps_monitor vs hostapd first-fragment differential, from qkaiser/broadcom-wps broadcom_fingerprint.py.

Associates as a WPS Enrollee, waits for WSC_START, then sends one first fragment that declares a total length
of 0 and carries no payload. EAP-Failure or a reason-0 deauth means wps_monitor; WSC_FRAG_ACK means hostapd.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_ENROLLEE, wps_assoc_ie

if TYPE_CHECKING:
    from wifit3.models import AccessPoint
    from wifit3.wlan.interface import WlanInterface

logger = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 10.0     # upstream DEFAULT_RESPONSE_TIMEOUT
RETRANSMIT_INTERVAL = 1.0   # upstream RETRANSMIT_INTERVAL
FRAME_DEAUTH = 0xC0
FRAME_DISASSOC = 0xA0
LEAVE_FRAME_NAMES = {FRAME_DEAUTH: "deauth", FRAME_DISASSOC: "disassoc"}
EAP_CODE_NAMES = {M.EAP_REQUEST: "EAP-Request", M.EAP_RESPONSE: "EAP-Response",
                  M.EAP_SUCCESS: "EAP-Success", M.EAP_FAILURE: "EAP-Failure"}
WSC_OPCODE_NAMES = {M.WSC_START: "WSC_START", M.WSC_ACK: "WSC_ACK", M.WSC_NACK: "WSC_NACK",
                    M.WSC_MSG: "WSC_MSG", M.WSC_DONE: "WSC_DONE", M.WSC_FRAG_ACK: "WSC_FRAG_ACK"}


class WpsImplementation(Enum):
    BROADCOM_WPS_MONITOR = auto()
    HOSTAPD = auto()
    INCONCLUSIVE = auto()


@dataclass
class ApEvent:
    eap: Optional[M.ParsedEap] = None
    leave_frame: int = 0        # FRAME_DEAUTH / FRAME_DISASSOC
    leave_reason: int = 0

    def describe(self) -> str:
        if self.leave_frame:
            return f"{LEAVE_FRAME_NAMES[self.leave_frame]} reason {self.leave_reason}"
        return describe_eap(self.eap)


def zero_length_first_fragment(eap_id: int) -> bytes:
    """EAP-Response WSC_MSG with Length + More Fragments set, a declared total of 0 and no payload."""
    body = (bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID + M.WFA_VENDOR_TYPE_SIMPLECONFIG
            + bytes([M.WSC_MSG, M.WSC_FLAG_LENGTH_FIELD | M.WSC_FLAG_MORE_FRAGMENTS])
            + (0).to_bytes(2, "big"))
    return M._eapol_eap(M.EAP_RESPONSE, eap_id, body)


def describe_eap(eap: M.ParsedEap) -> str:
    code = EAP_CODE_NAMES.get(eap.eap_code, f"EAP code {eap.eap_code}")
    if eap.eap_type == M.EAP_TYPE_EXPANDED:
        return f"{code}/{WSC_OPCODE_NAMES.get(eap.wsc_opcode, f'op-code 0x{eap.wsc_opcode:02x}')} (id {eap.eap_id})"
    if eap.eap_type:
        return f"{code} type {eap.eap_type} (id {eap.eap_id})"
    return f"{code} (id {eap.eap_id})"


async def next_event(transport: WlanTransport, timeout: float) -> Optional[ApEvent]:
    """The next EAP frame or deauth/disassoc from the AP within ``timeout``; None on silence."""
    deadline = asyncio.get_running_loop().time() + timeout
    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        frame = await transport.recv(remaining)
        if frame is None:
            return None
        if frame[0] in (FRAME_DEAUTH, FRAME_DISASSOC) and len(frame) >= 26:
            return ApEvent(leave_frame=frame[0], leave_reason=int.from_bytes(frame[24:26], "little"))
        parsed = M.parse_rx_frame(frame)
        if parsed is not None:
            return ApEvent(eap=parsed)
    return None


async def enter_wps(transport: WlanTransport, send: Callable[[bytes], Awaitable[None]]
                    ) -> tuple[Optional[int], Optional[int], Optional[ApEvent]]:
    """Upstream _start_wps: returns (Identity Request id, WSC_START id, the event that ended it early)."""
    last = M.eapol_start()
    await send(last)
    identity_id: Optional[int] = None
    deadline = time.monotonic() + RESPONSE_TIMEOUT
    while time.monotonic() < deadline:
        event = await next_event(transport, min(RETRANSMIT_INTERVAL, deadline - time.monotonic()))
        if event is None:
            if time.monotonic() < deadline:
                await send(last)
            continue
        if event.leave_frame or event.eap.is_eap_failure:
            return identity_id, None, event
        eap = event.eap
        if eap.is_identity_request:
            identity_id = eap.eap_id
            last = M.eap_identity_response(eap.eap_id, M.ENROLLEE_IDENTITY)
            await send(last)
        elif eap.eap_code == M.EAP_REQUEST and eap.wsc_opcode == M.WSC_START:
            return identity_id, eap.eap_id, None
    return identity_id, None, None


async def send_fragment(transport: WlanTransport, send: Callable[[bytes], Awaitable[None]],
                        eap_id: int) -> tuple[WpsImplementation, str]:
    """Upstream probe_first_fragment: returns (implementation, the AP's response)."""
    fragment = zero_length_first_fragment(eap_id)
    deadline = time.monotonic() + RESPONSE_TIMEOUT
    while time.monotonic() < deadline:
        await send(fragment)
        event = await next_event(transport, min(RETRANSMIT_INTERVAL, deadline - time.monotonic()))
        if event is None:
            continue
        if event.leave_frame:
            if event.leave_frame == FRAME_DEAUTH and event.leave_reason == 0:
                return WpsImplementation.BROADCOM_WPS_MONITOR, event.describe()
            return WpsImplementation.INCONCLUSIVE, event.describe()
        eap = event.eap
        if eap.is_identity_request or (eap.eap_code == M.EAP_REQUEST and eap.wsc_opcode == M.WSC_START):
            continue    # retransmit of an earlier request
        if eap.is_eap_failure:
            return WpsImplementation.BROADCOM_WPS_MONITOR, event.describe()
        if eap.eap_code == M.EAP_REQUEST and eap.wsc_opcode == M.WSC_FRAG_ACK:
            return WpsImplementation.HOSTAPD, event.describe()
        return WpsImplementation.INCONCLUSIVE, event.describe()
    return WpsImplementation.INCONCLUSIVE, f"no response in {RESPONSE_TIMEOUT:.0f}s"


async def probe_wps_implementation(iface: WlanInterface, ap: AccessPoint) -> WpsImplementation:
    """Associate as a WPS Enrollee and classify the AP's reply to a zero-length first fragment."""
    bssid = str_to_mac(ap.bssid.lower())
    await iface.set_channel(ap.channel)
    fake_mac = await iface.set_fake_mac(None, bssid)
    if fake_mac is None:
        logger.info("broadcom-wps: %s can't active-monitor", iface.name)
        return WpsImplementation.INCONCLUSIVE
    our_mac = str_to_mac(fake_mac)
    transport = WlanTransport(iface, bssid, our_mac)

    async def send(payload_1x: bytes) -> None:
        await transport.send_no_wait(M.build_data_frame(bssid, our_mac, bssid, payload_1x))

    assoc = Association(iface, ap.bssid.lower(), ap.ssid or "", ap.channel, our_mac=our_mac,
                        assoc_trailer_ies=wps_assoc_ie(WPS_REQ_ENROLLEE, False))
    assoc.start()
    transport.start()
    try:
        if not await assoc.associate():
            logger.info("broadcom-wps: no association (%s)", assoc.fail_reason)
            return WpsImplementation.INCONCLUSIVE
        identity_id, wsc_start_id, early = await enter_wps(transport, send)
        if early is not None:
            logger.info("broadcom-wps: AP ended WPS entry with %s", early.describe())
            return WpsImplementation.INCONCLUSIVE
        fragment_id = wsc_start_id if wsc_start_id is not None else identity_id
        if fragment_id is None:
            logger.info("broadcom-wps: no Identity Request to take an EAP id from")
            return WpsImplementation.INCONCLUSIVE
        implementation, response = await send_fragment(transport, send, fragment_id)
        logger.info("broadcom-wps: %s (WSC_START id %s, AP response %s)",
                    implementation.name, wsc_start_id, response)
        return implementation
    finally:
        await iface.send_no_wait(build_client_leaving(bssid, our_mac))
        transport.stop()
        assoc.stop()
        await iface.clear_fake_mac()
