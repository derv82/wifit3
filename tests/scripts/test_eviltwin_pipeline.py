"""Offline end-to-end: M1 -> client M2 (real MIC) -> parse -> assemble -> hc22000 -> crack.

Exercises the exact code fake_ap.py and crack.py run, with no hardware: proves the WPA2 capture
pipeline is self-consistent before the RF test can add only radio failures on top.
"""
import importlib.util
import sys
from pathlib import Path

from wifit3.dot11.ap import eapol_m1
from wifit3.dot11.eapol import eapol_key, set_mic, data_header, LLC_SNAP_EAPOL
from wifit3.dot11.ie import GENERIC_RSN_IE
from wifit3.dot11.parser import WlanFrameParser
from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import eapol_hashlines
from wifit3.models import Handshake, HandshakeMessage

_ROOT = Path(__file__).resolve().parents[2]


def _load_crack():
    sys.path.insert(0, str(_ROOT / "scripts" / "ap"))
    spec = importlib.util.spec_from_file_location("ap_crack", _ROOT / "scripts" / "ap" / "crack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client_m2(bssid, client, ssid, psk, anonce, snonce, replay):
    zeroed = eapol_key(key_info=0x010A, key_len=0, replay=replay, nonce=snonce, key_data=GENERIC_RSN_IE)
    mic = wpa_psk.mic_for(psk, ssid, bssid, client, anonce, snonce, zeroed)
    payload = set_mic(zeroed, mic)
    return data_header(to_ds=True, bssid=bssid, client=client) + LLC_SNAP_EAPOL + payload


def test_full_capture_pipeline_cracks():
    ssid, psk = "GL-Test", "correcthorse"
    bssid = bytes.fromhex("9483c48c3f78")
    client = bytes.fromhex("02aabbccddee")
    anonce = bytes(range(32))
    snonce = bytes(range(32, 64))

    m1 = eapol_m1(bssid, client, anonce, replay=1)
    m2 = _client_m2(bssid, client, ssid, psk, anonce, snonce, replay=1)

    parsed = WlanFrameParser.parse_80211_frame(m2, 0)
    assert parsed is not None and parsed.type == "eapol"
    assert parsed.msg_num == 2
    assert parsed.nonce == snonce and len(parsed.mic) == 16

    hs = Handshake(bssid="94:83:c4:8c:3f:78", client_mac="02:aa:bb:cc:dd:ee",
                   beacon_frame=b"B", akm_offered=[2, 8])
    hs.messages.append(HandshakeMessage(raw=m1, msg_num=1, replay_hex="0000000000000001",
                                        nonce=anonce, mic=bytes(16), key_data_len=0,
                                        eapol_payload=m1[32:], timestamp=1.0))
    hs.messages.append(HandshakeMessage(raw=m2, msg_num=2, replay_hex=parsed.replay_counter.hex(),
                                        nonce=parsed.nonce, mic=parsed.mic,
                                        key_data_len=parsed.key_data_len,
                                        eapol_payload=parsed.payload, akm=2, timestamp=1.1))

    lines = eapol_hashlines(ssid, hs)
    assert len(lines) == 1 and lines[0].startswith("WPA*02*")

    crack = _load_crack()
    assert crack._verify_line(lines[0], [psk]) == (ssid, psk)
    assert crack._verify_line(lines[0], ["wrongpass"]) is None
