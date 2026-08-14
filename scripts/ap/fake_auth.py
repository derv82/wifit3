"""Victim-client simulator: drive the full client side of the EvilTwin capture end-to-end.

Tunes to the target channel, follows a Channel Switch Announcement onto the decoy channel,
auths + associates (WPA2-PSK RSN), receives M1, and replies with a real-MIC M2 derived from a
known PSK. Two uses: (1) against the real AP (--no-csa, decoy = real channel) it should receive
M3, which proves our WPA2 MIC math is correct because only a valid MIC advances the 4-way; (2)
against fake_ap.py's twin it exercises the responder so fake_ap captures a crackable M2.

  uv run python scripts/ap/fake_auth.py --card <substr> --bssid <ap> --ssid <name> --psk <psk> \
      [--channel 1] [--target-channel 11] [--no-csa] [--debug]

Output prefixes: [+] ok   [*] step   [-] a problem.
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.device.manager import wlan_ifaces
from wifit3.chips.driver import FakeMacSupport
from wifit3.campaigns.auth_assoc import Association, random_client_mac, str_to_mac
from wifit3.dot11.eapol import eapol_key, set_mic, data_header, LLC_SNAP_EAPOL
from wifit3.dot11.ie import GENERIC_RSN_IE
from wifit3.crack import wpa_psk

_M2_KEY_INFO = 0x010A          # Pairwise + Key MIC + key descriptor version 2
_ELEMID_CSA = 0x25


def _mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _csa_target(beacon: bytes, bssid: str) -> int | None:
    """The channel a CSA element in this beacon points at, or None if there's no CSA."""
    tags = beacon[36:]
    ptr = 0
    while ptr + 2 <= len(tags):
        end = ptr + 2 + tags[ptr + 1]
        if end > len(tags):
            break
        if tags[ptr] == _ELEMID_CSA and end - ptr >= 5:
            return tags[ptr + 3]
        ptr = end
    return None


async def follow_csa(iface, bssid: str, target_channel: int, timeout: float) -> int | None:
    """Sit on the target channel and return the channel a CSA beacon herds us to, or None."""
    await iface.set_channel(target_channel)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _on(pkt) -> None:
        if pkt.type == "beacon" and pkt.bssid == bssid and pkt.raw and not fut.done():
            ch = _csa_target(pkt.raw, bssid)
            if ch is not None:
                fut.set_result(ch)

    iface.register_rx_callback(_on)
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        iface.unregister_rx_callback(_on)


class FourWayClient:
    """Watches for M1 (AP->us) and answers with a valid-MIC M2; notes M3 if it comes back."""

    def __init__(self, iface, bssid: bytes, our_mac: bytes, ssid: str, psk: str, rsn_ie: bytes):
        self.iface = iface
        self.bssid = bssid
        self.our_mac = our_mac
        self.ssid = ssid
        self.psk = psk
        self.rsn_ie = rsn_ie
        loop = asyncio.get_running_loop()
        self.m1 = loop.create_future()
        self.m3 = loop.create_future()

    def rx(self, pkt) -> None:
        if pkt.type != "eapol" or len(pkt.raw) < 16:
            return
        if pkt.raw[4:10] != self.our_mac or pkt.raw[10:16] != self.bssid:
            return
        msg = getattr(pkt, "msg_num", 0)
        if msg == 1 and not self.m1.done():
            self.m1.set_result(pkt)
        elif msg == 3 and not self.m3.done():
            self.m3.set_result(pkt)

    def build_m2(self, m1) -> bytes:
        anonce = m1.nonce
        snonce = os.urandom(32)
        replay = int.from_bytes(m1.replay_counter or b"\x00" * 8, "big")
        zeroed = eapol_key(key_info=_M2_KEY_INFO, key_len=0, replay=replay, nonce=snonce,
                           key_data=self.rsn_ie)
        mic = wpa_psk.mic_for(self.psk, self.ssid, self.bssid, self.our_mac, anonce, snonce, zeroed)
        payload = set_mic(zeroed, mic)
        return data_header(to_ds=True, bssid=self.bssid, client=self.our_mac) + LLC_SNAP_EAPOL + payload


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)
    ifaces = wlan_ifaces()
    iface = next((i for i in ifaces if a.card.lower() in (i.description or "").lower()), None)
    if iface is None:
        print(f"[-] no card matching '{a.card}'. plugged in:")
        for i in ifaces:
            print(f"      {i.description}")
        return

    print(f"[*] connecting {iface.description}...")
    if not await iface.connect():
        print("[-] connect failed")
        return

    decoy = a.channel
    if not a.no_csa:
        print(f"[*] waiting for a CSA from {a.bssid} on ch {a.target_channel}...")
        herded = await follow_csa(iface, a.bssid, a.target_channel, timeout=a.csa_timeout)
        if herded is None:
            print(f"[-] no CSA seen in {a.csa_timeout}s; falling back to ch {a.channel}")
        else:
            decoy = herded
            print(f"[+] CSA herded us to ch {decoy}")

    our_mac = random_client_mac()
    if iface.driver.FAKE_MAC is FakeMacSupport.SPOOFABLE:
        armed = await iface.set_fake_mac(our_mac, str_to_mac(a.bssid))
        if armed:
            our_mac = str_to_mac(armed)
    print(f"[*] associating to {a.bssid} ('{a.ssid}') on ch {decoy} as {_mac_str(our_mac)}")

    client = FourWayClient(iface, str_to_mac(a.bssid), our_mac, a.ssid, a.psk, GENERIC_RSN_IE)
    iface.register_rx_callback(client.rx)
    assoc = Association(iface, a.bssid, a.ssid, decoy, our_mac=our_mac,
                        assoc_trailer_ies=GENERIC_RSN_IE)
    assoc.start()
    try:
        if not await assoc.associate():
            print(f"[-] association failed: {assoc.fail_reason}")
            return
        print("[+] associated (auth + assoc accepted)")

        try:
            m1 = await asyncio.wait_for(client.m1, timeout=a.m1_timeout)
        except asyncio.TimeoutError:
            print("[-] no M1 from the AP")
            return
        print(f"[+] M1 received (ANonce {m1.nonce[:4].hex()}..., replay {(m1.replay_counter or b'').hex()})")

        m2 = client.build_m2(m1)
        for _ in range(4):
            await iface.send_no_wait(m2)
            await asyncio.sleep(0.05)
        print(f"[+] M2 sent (valid MIC over PSK '{a.psk}')")

        try:
            await asyncio.wait_for(client.m3, timeout=a.m3_timeout)
            print("[+] M3 received: the AP accepted our MIC -> WPA2 crypto VERIFIED end-to-end")
        except asyncio.TimeoutError:
            print("[*] no M3 (expected against fake_ap.py's twin; it only captures M2)")
    finally:
        assoc.stop()
        iface.unregister_rx_callback(client.rx)
        try:
            if iface.driver.FAKE_MAC is FakeMacSupport.SPOOFABLE:
                await iface.clear_fake_mac()
        except Exception:                                     # noqa: BLE001
            pass
        await iface.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="EvilTwin victim-client simulator")
    p.add_argument("--card", required=True, help="substring of the client card")
    p.add_argument("--bssid", required=True, help="target/twin BSSID to associate with")
    p.add_argument("--ssid", required=True, help="SSID (needed for the PMK)")
    p.add_argument("--psk", required=True, help="the PSK to derive a valid M2 MIC")
    p.add_argument("--channel", type=int, default=1, help="decoy channel (fallback if no CSA)")
    p.add_argument("--target-channel", type=int, default=11, help="the target AP's real channel")
    p.add_argument("--no-csa", action="store_true", help="skip CSA-follow, associate on --channel")
    p.add_argument("--csa-timeout", type=float, default=15.0)
    p.add_argument("--m1-timeout", type=float, default=5.0)
    p.add_argument("--m3-timeout", type=float, default=3.0)
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
