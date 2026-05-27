"""Prove the WpsRegistrar state machine + the two headline outcomes offline.

A minimal in-process WSC *enrollee* (the AP side) that knows the real PIN + PSK
runs against the real WpsRegistrar over a loopback transport. This exercises:
  * the EAPOL-Start → Identity → M1..M7 exchange,
  * the split-PIN oracle (first-half / second-half / success), and
  * PSK extraction from M7's Encrypted Settings.

The enrollee builds its own M1/M3/M5/M7 with hand-rolled TLVs (not the
registrar's builders), so this isn't one code path graded against itself. The
crypto primitives are anchored externally in test_wsc_crypto.py; the on-air
correctness against a real AP's independent stack is what wps_probe.py proves.
"""

import asyncio
import os

import pytest

from wifit3.engine.attacks.wps import messages as M
from wifit3.engine.attacks.wps import wsc_crypto as wc
from wifit3.engine.attacks.wps.registrar import WpsRegistrar, PinResult

BSSID = bytes.fromhex("3421090001ff")
STA = bytes.fromhex("02aabbccddee")
MAC_E = BSSID                      # AP-as-enrollee uses its BSSID as its MAC


class _QueueTransport:
    def __init__(self, tx: asyncio.Queue, rx: asyncio.Queue):
        self._tx, self._rx = tx, rx

    async def send(self, frame: bytes) -> None:
        await self._tx.put(frame)

    async def recv(self, timeout: float):
        try:
            return await asyncio.wait_for(self._rx.get(), timeout)
        except asyncio.TimeoutError:
            return None


class FakeEnrollee:
    """The AP side of one WPS exchange. Knows the real PIN + PSK."""

    def __init__(self, transport, real_pin: str, psk: str, ssid: str):
        self.t = transport
        self.real_pin = real_pin
        self.psk = psk.encode()
        self.ssid = ssid.encode()
        self.priv, self.pke = wc.dh_generate_keypair()
        self.nonce_e = os.urandom(16)
        self.e_s1 = os.urandom(16)
        self.e_s2 = os.urandom(16)
        self.authkey = self.keywrapkey = None
        self.psk1 = self.psk2 = None
        self.pkr = None
        self.r_hash2 = None
        self.last_recv = b""        # for the (cosmetic) Authenticator
        self._eap_id = 1

    # -- enrollee-side framing (EAP-Request, FromDS) -------------------------
    def _next_id(self) -> int:
        self._eap_id += 1
        return self._eap_id

    def _req_wsc(self, eap_id: int, attrs: bytes) -> bytes:
        exp = (bytes([M.EAP_TYPE_EXPANDED]) + M.WFA_VENDOR_ID
               + M.WFA_VENDOR_TYPE_SIMPLECONFIG + bytes([M.WSC_MSG, 0x00]) + attrs)
        import struct
        eap = struct.pack(">BBH", M.EAP_REQUEST, eap_id, 4 + len(exp)) + exp
        x = struct.pack(">BBH", 1, 0, len(eap)) + eap
        return b"\x08\x02\x00\x00" + STA + BSSID + BSSID + b"\x00\x00" + M._LLC_SNAP_EAPOL + x

    def _req_identity(self, eap_id: int) -> bytes:
        import struct
        eap = struct.pack(">BBH", M.EAP_REQUEST, eap_id, 4 + 1) + bytes([M.EAP_TYPE_IDENTITY])
        x = struct.pack(">BBH", 1, 0, len(eap)) + eap
        return b"\x08\x02\x00\x00" + STA + BSSID + BSSID + b"\x00\x00" + M._LLC_SNAP_EAPOL + x

    def _enc(self, inner: bytes) -> bytes:
        kwa = wc.key_wrap_authenticator(self.authkey, inner)
        plain = wc.pkcs5_pad(inner + M.tlv(M.ATTR_KEY_WRAP_AUTH, kwa))
        iv = os.urandom(16)
        return iv + wc.aes128_cbc_encrypt(self.keywrapkey, iv, plain)

    @staticmethod
    def _is_eapol_start(frame: bytes) -> bool:
        pos = M._find_eapol(frame)
        return pos is not None and pos < len(frame) and frame[pos + 1] == M.DOT1X_TYPE_EAPOL_START

    async def run(self):
        while True:
            frame = await self.t.recv(2.0)
            if frame is None:
                return
            if self._is_eapol_start(frame):
                await self.t.send(self._req_identity(self._eap_id))
                continue
            p = M.parse_rx_frame(frame)
            if p is None:
                continue
            if p.eap_type == M.EAP_TYPE_IDENTITY and p.eap_code == M.EAP_RESPONSE:
                await self._send_m1()
            elif p.wsc_msg_type == M.WPS_M2:
                await self._on_m2(p)
            elif p.wsc_msg_type == M.WPS_M4:
                if not await self._on_m4(p):
                    return                 # NACKed → exchange over
            elif p.wsc_msg_type == M.WPS_M6:
                await self._on_m6(p)
                return                     # M7 or NACK is terminal
            elif p.wsc_msg_type == M.WPS_WSC_NACK:
                return

    async def _send_m1(self):
        attrs = (
            M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M1)
            + M.tlv(M.ATTR_ENROLLEE_NONCE, self.nonce_e)
            + M.tlv(M.ATTR_PUBLIC_KEY, self.pke)
            + M.tlv(M.ATTR_MAC_ADDR, MAC_E)
        )
        self.last_recv = attrs
        await self.t.send(self._req_wsc(self._next_id(), attrs))

    async def _on_m2(self, p):
        self.pkr = p.attrs[M.ATTR_PUBLIC_KEY]
        nonce_r = p.attrs[M.ATTR_REGISTRAR_NONCE]
        shared = wc.dh_shared_secret(self.pkr, self.priv)
        self.authkey, self.keywrapkey, _ = wc.derive_keys(shared, self.nonce_e, MAC_E, nonce_r)
        self.psk1, self.psk2 = wc.derive_psk(self.authkey, self.real_pin)
        e_hash1 = wc.e_or_r_hash(self.authkey, self.e_s1, self.psk1, self.pke, self.pkr)
        e_hash2 = wc.e_or_r_hash(self.authkey, self.e_s2, self.psk2, self.pke, self.pkr)
        body = (
            M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M3)
            + M.tlv(M.ATTR_REGISTRAR_NONCE, nonce_r)
            + M.tlv(M.ATTR_E_HASH1, e_hash1) + M.tlv(M.ATTR_E_HASH2, e_hash2)
        )
        auth = wc.authenticator(self.authkey, self.last_recv, body)
        attrs = body + M.tlv(M.ATTR_AUTHENTICATOR, auth)
        self.last_recv = p.raw_wsc_attrs
        await self.t.send(self._req_wsc(self._next_id(), attrs))

    async def _on_m4(self, p) -> bool:
        # The registrar revealed R-S1 + committed R-Hash1/2. Verify the first half.
        self.r_hash2 = p.attrs[M.ATTR_R_HASH2]
        enc = p.attrs[M.ATTR_ENCR_SETTINGS]
        plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(self.keywrapkey, enc[:16], enc[16:]))
        r_s1 = M.parse_tlvs(plain)[M.ATTR_R_SNONCE1]
        ok = wc.e_or_r_hash(self.authkey, r_s1, self.psk1, self.pke, self.pkr) == p.attrs[M.ATTR_R_HASH1]
        if not ok:
            await self.t.send(self._req_wsc(self._next_id(),
                              M.build_wsc_nack(self.nonce_e, b"\x00" * 16)))
            return False
        body = (
            M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M5)
            + M.tlv(M.ATTR_ENCR_SETTINGS, self._enc(M.tlv(M.ATTR_E_SNONCE1, self.e_s1)))
        )
        self.last_recv = p.raw_wsc_attrs
        await self.t.send(self._req_wsc(self._next_id(), body))
        return True

    async def _on_m6(self, p):
        enc = p.attrs[M.ATTR_ENCR_SETTINGS]
        plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(self.keywrapkey, enc[:16], enc[16:]))
        r_s2 = M.parse_tlvs(plain)[M.ATTR_R_SNONCE2]
        ok = wc.e_or_r_hash(self.authkey, r_s2, self.psk2, self.pke, self.pkr) == self.r_hash2
        if not ok:
            await self.t.send(self._req_wsc(self._next_id(),
                              M.build_wsc_nack(self.nonce_e, b"\x00" * 16)))
            return
        # Second half correct → disclose the AP's config (the prize) in M7.
        inner = (M.tlv(M.ATTR_E_SNONCE2, self.e_s2)
                 + M.tlv(M.ATTR_SSID, self.ssid) + M.tlv(M.ATTR_NETWORK_KEY, self.psk))
        body = (
            M.tlv_u8(M.ATTR_VERSION, 0x10) + M.tlv_u8(M.ATTR_MSG_TYPE, M.WPS_M7)
            + M.tlv(M.ATTR_ENCR_SETTINGS, self._enc(inner))
        )
        await self.t.send(self._req_wsc(self._next_id(), body))


async def _run(real_pin, guess, psk="supersecret123", ssid="TestNet"):
    a, b = asyncio.Queue(), asyncio.Queue()
    enrollee = FakeEnrollee(_QueueTransport(b, a), real_pin, psk, ssid)
    reg = WpsRegistrar(_QueueTransport(a, b), BSSID, STA, msg_timeout=1.0, eapol_start_timeout=1.0)
    task = asyncio.create_task(enrollee.run())
    try:
        return await asyncio.wait_for(reg.try_pin(guess), timeout=5.0)
    finally:
        task.cancel()


async def test_correct_pin_recovers_psk():
    out = await _run("12345670", "12345670")
    assert out.result is PinResult.SUCCESS
    assert out.psk == "supersecret123"
    assert out.ssid == "TestNet"
    # The campaign's headline log line:
    line = f"WPS PIN {out.pin} CORRECT, PASSWORD: {out.psk}"
    assert line == "WPS PIN 12345670 CORRECT, PASSWORD: supersecret123"


async def test_wrong_first_half():
    out = await _run("12345670", "99995670")
    assert out.result is PinResult.FIRST_HALF_WRONG
    assert not out.first_half_ok
    assert f"WPS PIN {out.pin} incorrect" == "WPS PIN 99995670 incorrect"


async def test_correct_first_half_wrong_second():
    out = await _run("12345670", "12349999")
    assert out.result is PinResult.SECOND_HALF_WRONG
    assert out.first_half_ok        # the first-half oracle says 1234 is right


async def test_psk_only_revealed_on_full_match():
    out = await _run("12345670", "12340000")
    assert out.psk is None
