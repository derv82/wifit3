"""Prove the WpsEnrollee (PBC) state machine offline.

A minimal in-process WSC *Registrar* (the AP side, post-button-press) holding a
known PSK runs against the real WpsEnrollee over a loopback. This exercises the
mirror exchange — EAPOL-Start → Identity → WSC_Start → M1 → M2 → M3 → M4 → M5 →
M6 → M7 → M8 — and the PSK extraction from M8's nested Credential.

The registrar independently recomputes E-Hash1 from the E-S1 we reveal in M5 and
asserts it matches the M3 we sent — so this validates our enrollee's PBC E-hash
crypto, not just that the plumbing connects. On-air correctness against a real
AP's stack is what pbc_probe.py will prove.
"""

import asyncio
import os
import struct

from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc import crypto as wc
from wifit3.engine.attacks.wps.enrollee import WpsEnrollee
from wifit3.engine.attacks.wps.registrar import PinResult

BSSID = bytes.fromhex("3421090001ff")
STA = bytes.fromhex("02aabbccddee")


class _QueueTransport:
    def __init__(self, tx: asyncio.Queue, rx: asyncio.Queue):
        self._tx, self._rx = tx, rx

    async def send(self, frame: bytes, wait_for_ack: float = 0.0,
                   max_resends: int = 0) -> bool:
        await self._tx.put(frame)
        return True

    async def send_until_ack(self, frame: bytes, max_retries: int = 0) -> bool:
        await self._tx.put(frame)
        return True

    async def send_no_wait(self, frame: bytes) -> bool:
        await self._tx.put(frame)
        return True

    async def recv(self, timeout: float):
        try:
            return await asyncio.wait_for(self._rx.get(), timeout)
        except asyncio.TimeoutError:
            return None


class FakeRegistrar:
    """The AP side after the WPS button is pressed (PBC registrar)."""

    def __init__(self, transport, psk: str, ssid: str):
        self.t = transport
        self.psk = psk.encode()
        self.ssid = ssid.encode()
        self.priv, self.pkr = wc.dh_generate_keypair()
        self.nonce_r = os.urandom(16)
        self.uuid_r = os.urandom(16)
        self.r_s1, self.r_s2 = os.urandom(16), os.urandom(16)
        self.pke = self.nonce_e = self.mac_e = None
        self.authkey = self.keywrapkey = self.psk1 = self.psk2 = None
        self.e_hash1 = None
        self.last_recv = b""
        self._id = 1
        self.e_hash1_verified = False

    def _nid(self):
        self._id += 1
        return self._id

    def _req(self, eap_id, opcode, attrs=b""):
        exp = (bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID
               + M.WFA_VENDOR_TYPE_SIMPLECONFIG + bytes([opcode, 0x00]) + attrs)
        eap = struct.pack(">BBH", M.EAP_REQUEST, eap_id, 4 + len(exp)) + exp
        x = struct.pack(">BBH", 1, 0, len(eap)) + eap
        return b"\x08\x02\x00\x00" + STA + BSSID + BSSID + b"\x00\x00" + M._LLC_SNAP_EAPOL + x

    def _req_identity(self, eap_id):
        eap = struct.pack(">BBH", M.EAP_REQUEST, eap_id, 5) + bytes([M.EAP_TYPE_IDENTITY])
        x = struct.pack(">BBH", 1, 0, len(eap)) + eap
        return b"\x08\x02\x00\x00" + STA + BSSID + BSSID + b"\x00\x00" + M._LLC_SNAP_EAPOL + x

    @staticmethod
    def _is_eapol_start(frame):
        pos = M._find_eapol(frame)
        return pos is not None and frame[pos + 1] == M.DOT1X_TYPE_EAPOL_START

    async def run(self):
        while True:
            frame = await self.t.recv(2.0)
            if frame is None:
                return
            if self._is_eapol_start(frame):
                await self.t.send(self._req_identity(self._id))
                continue
            p = M.parse_rx_frame(frame)
            if p is None:
                continue
            if p.eap_type == M.EAP_TYPE_IDENTITY and p.eap_code == M.EAP_RESPONSE:
                await self.t.send(self._req(self._nid(), M.WSC_START))  # kick off M1
            elif p.wsc_msg_type == M.WPS_M1:
                await self._on_m1(p)
            elif p.wsc_msg_type == M.WPS_M3:
                await self._on_m3(p)
            elif p.wsc_msg_type == M.WPS_M5:
                await self._on_m5(p)
            elif p.wsc_msg_type == M.WPS_M7:
                await self._on_m7(p)
            elif p.wsc_msg_type == M.WPS_WSC_DONE:
                return

    async def _on_m1(self, p):
        self.pke = p.attrs[M.ATTR_PUBLIC_KEY]
        self.nonce_e = p.attrs[M.ATTR_ENROLLEE_NONCE]
        self.mac_e = p.attrs[M.ATTR_MAC_ADDR]
        shared = wc.dh_shared_secret(self.pke, self.priv)
        self.authkey, self.keywrapkey, _ = wc.derive_keys(shared, self.nonce_e, self.mac_e, self.nonce_r)
        self.psk1, self.psk2 = wc.derive_psk(self.authkey, M.PBC_PASSWORD)
        self.last_recv = p.raw_wsc_attrs
        m2 = M.build_m2(self.nonce_e, self.nonce_r, self.uuid_r, self.pkr, self.authkey, p.raw_wsc_attrs)
        await self.t.send(self._req(self._nid(), M.WSC_MSG, m2))

    async def _on_m3(self, p):
        self.e_hash1 = p.attrs[M.ATTR_E_HASH1]      # remember to verify against revealed E-S1
        m4 = M.build_m4(self.nonce_e, self.r_s1, self.r_s2, self.psk1, self.psk2,
                        self.pke, self.pkr, self.authkey, self.keywrapkey, p.raw_wsc_attrs)
        await self.t.send(self._req(self._nid(), M.WSC_MSG, m4))

    async def _on_m5(self, p):
        enc = p.attrs[M.ATTR_ENCR_SETTINGS]
        plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(self.keywrapkey, enc[:16], enc[16:]))
        e_s1 = M.parse_tlvs(plain)[M.ATTR_E_SNONCE1]
        # The enrollee's M3 E-Hash1 must reproduce from the E-S1 it just revealed.
        self.e_hash1_verified = (
            wc.e_or_r_hash(self.authkey, e_s1, self.psk1, self.pke, self.pkr) == self.e_hash1
        )
        m6 = M.build_m6(self.nonce_e, self.r_s2, self.authkey, self.keywrapkey, p.raw_wsc_attrs)
        await self.t.send(self._req(self._nid(), M.WSC_MSG, m6))

    async def _on_m7(self, p):
        # Disclose the network credential in M8 (nested ATTR_CRED).
        cred = (M.tlv(M.ATTR_SSID, self.ssid) + M.tlv(M.ATTR_MAC_ADDR, BSSID)
                + M.tlv(M.ATTR_NETWORK_KEY, self.psk))
        inner = M.tlv(M.ATTR_CRED, cred)
        kwa = wc.key_wrap_authenticator(self.authkey, inner)
        plain = wc.pkcs5_pad(inner + M.tlv(M.ATTR_KEY_WRAP_AUTH, kwa))
        iv = os.urandom(16)
        enc = iv + wc.aes128_cbc_encrypt(self.keywrapkey, iv, plain)
        body = (M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M8)
                + M.tlv(M.ATTR_ENROLLEE_NONCE, self.nonce_e) + M.tlv(M.ATTR_ENCR_SETTINGS, enc))
        m8 = body + M.tlv(M.ATTR_AUTHENTICATOR, wc.authenticator(self.authkey, p.raw_wsc_attrs, body))
        await self.t.send(self._req(self._nid(), M.WSC_MSG, m8))


async def _run(psk="correct horse 9", ssid="HomeNet"):
    a, b = asyncio.Queue(), asyncio.Queue()
    reg = FakeRegistrar(_QueueTransport(b, a), psk, ssid)
    enr = WpsEnrollee(_QueueTransport(a, b), BSSID, STA,
                      msg_timeout=1.0, eapol_start_timeout=1.0)
    task = asyncio.create_task(reg.run())
    try:
        out = await asyncio.wait_for(enr.run(), timeout=5.0)
        return out, reg
    finally:
        task.cancel()


async def test_pbc_capture_recovers_psk():
    out, reg = await _run(psk="correct horse 9", ssid="HomeNet")
    assert out.result is PinResult.SUCCESS
    assert out.psk == "correct horse 9"
    assert out.ssid == "HomeNet"
    # The headline log line the orchestrator will emit:
    assert f"PBC: captured PSK for {out.ssid}: {out.psk}" == \
        "PBC: captured PSK for HomeNet: correct horse 9"


async def test_pbc_enrollee_ehash_is_valid():
    # The registrar independently verified our M3 E-Hash1 against the E-S1 we
    # revealed — proves our enrollee PBC crypto (PSK from "00000000") is right.
    _out, reg = await _run()
    assert reg.e_hash1_verified


async def test_pbc_psk_with_binary_safe_decode():
    out, _ = await _run(psk="p@ss:w0rd!", ssid="café")
    assert out.psk == "p@ss:w0rd!"
    assert out.ssid == "café"


async def test_pbc_enrollee_aborts_on_should_stop():
    """A cooperative stop (Campaign.stopped, polled via should_stop) bails the
    enrollee with ABORTED before it blocks on recv — this is what lets the 'Stop
    PBC' button free the radio promptly instead of running to the ~30 s deadline."""
    a, b = asyncio.Queue(), asyncio.Queue()
    enr = WpsEnrollee(_QueueTransport(a, b), BSSID, STA,
                      msg_timeout=1.0, eapol_start_timeout=1.0,
                      should_stop=lambda: True)
    out = await asyncio.wait_for(enr.run(), timeout=5.0)
    assert out.result is PinResult.ABORTED
    assert "stopped" in out.detail
