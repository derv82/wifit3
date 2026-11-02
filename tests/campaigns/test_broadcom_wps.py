import struct
from typing import Optional

from wifit3.campaigns.probe.broadcom_wps import (FRAME_DEAUTH, WpsImplementation, enter_wps, send_fragment,
                                                 zero_length_first_fragment)
from wifit3.dot11.wsc import messages as M

BSSID = bytes.fromhex("020000000001")
STA = bytes.fromhex("02aabbccddee")


def _eapol_frame(eap: bytes) -> bytes:
    x = struct.pack(">BBH", 1, 0, len(eap)) + eap
    return b"\x08\x02\x00\x00" + STA + BSSID + BSSID + b"\x00\x00" + M.LLC_SNAP_EAPOL + x


def _wsc_request(eap_id: int, opcode: int) -> bytes:
    exp = bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID + M.WFA_VENDOR_TYPE_SIMPLECONFIG + bytes([opcode, 0x00])
    return _eapol_frame(struct.pack(">BBH", M.EAP_REQUEST, eap_id, 4 + len(exp)) + exp)


def _identity_request(eap_id: int) -> bytes:
    return _eapol_frame(struct.pack(">BBH", M.EAP_REQUEST, eap_id, 5) + bytes([M.EAP_TYPE_IDENTITY]))


def _eap_failure(eap_id: int) -> bytes:
    return _eapol_frame(struct.pack(">BBH", M.EAP_FAILURE, eap_id, 4))


def _deauth(reason: int) -> bytes:
    return bytes([FRAME_DEAUTH, 0, 0, 0]) + STA + BSSID + BSSID + b"\x00\x00" + reason.to_bytes(2, "little")


class ScriptedTransport:
    def __init__(self, replies: list[bytes]):
        self.replies = replies

    async def recv(self, timeout: float) -> Optional[bytes]:
        return self.replies.pop(0) if self.replies else None


class RecordingSend:
    def __init__(self):
        self.sent: list[bytes] = []

    async def __call__(self, payload_1x: bytes) -> None:
        self.sent.append(payload_1x)


def test_zero_length_first_fragment_sets_length_and_more_fragments_with_no_payload():
    fragment = zero_length_first_fragment(7)
    eap = fragment[4:]
    assert eap[:2] == bytes([M.EAP_RESPONSE, 7])
    assert eap[12] == M.WSC_MSG
    assert eap[13] == M.WSC_FLAG_LENGTH_FIELD | M.WSC_FLAG_MORE_FRAGMENTS
    assert eap[14:] == b"\x00\x00"


async def test_enter_wps_answers_identity_as_enrollee_and_returns_wsc_start_id():
    send = RecordingSend()
    transport = ScriptedTransport([_identity_request(1), _wsc_request(2, M.WSC_START)])
    identity_id, wsc_start_id, early = await enter_wps(transport, send)
    assert (identity_id, wsc_start_id, early) == (1, 2, None)
    assert send.sent[1] == M.eap_identity_response(1, M.ENROLLEE_IDENTITY)


async def test_eap_failure_after_fragment_is_broadcom_wps_monitor():
    implementation, _ = await send_fragment(ScriptedTransport([_eap_failure(2)]), RecordingSend(), 2)
    assert implementation is WpsImplementation.BROADCOM_WPS_MONITOR


async def test_reason_0_deauth_after_fragment_is_broadcom_wps_monitor():
    implementation, response = await send_fragment(ScriptedTransport([_deauth(0)]), RecordingSend(), 2)
    assert implementation is WpsImplementation.BROADCOM_WPS_MONITOR
    assert response == "deauth reason 0"


async def test_frag_ack_after_fragment_is_hostapd():
    transport = ScriptedTransport([_wsc_request(3, M.WSC_FRAG_ACK)])
    implementation, _ = await send_fragment(transport, RecordingSend(), 2)
    assert implementation is WpsImplementation.HOSTAPD


async def test_other_deauth_reason_after_fragment_is_inconclusive():
    implementation, response = await send_fragment(ScriptedTransport([_deauth(7)]), RecordingSend(), 2)
    assert implementation is WpsImplementation.INCONCLUSIVE
    assert response == "deauth reason 7"


async def test_retransmitted_wsc_start_is_skipped_before_the_classifying_reply():
    transport = ScriptedTransport([_wsc_request(2, M.WSC_START), _eap_failure(2)])
    send = RecordingSend()
    implementation, _ = await send_fragment(transport, send, 2)
    assert implementation is WpsImplementation.BROADCOM_WPS_MONITOR
    assert send.sent == [zero_length_first_fragment(2)] * 2
