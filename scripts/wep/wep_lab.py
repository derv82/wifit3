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

    uv run python scripts/wep/wep_lab.py --essid myssid --channel 6 --pass abcde
    uv run python scripts/wep/wep_lab.py --essid myssid --channel 6 --pass 6162636465 --generate 120
    uv run python scripts/wep/wep_lab.py --essid myssid --channel 6 --pass abcde --no-active-monitor
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
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.dot11.auth_assoc import auth_req, assoc_req
from wifit3.dot11.wep.crypto import arp_request_plaintext, wep_encrypt
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket

BROADCAST = b"\xff" * 6


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
    if args.own_mac and iface.mac_address:
        our_mac = _mac(iface.mac_address)   # FIXED_MAC cards only HW-ACK their own address
    else:
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
    if args.generate and args.strategy == "adaptive":
        # Climb-with-peak-memory: push the per-window inject count UP while echoes keep rising,
        # remember the best (echo_rate, target), and settle back to it once going higher stops
        # helping; re-probe occasionally. Handles both regimes with no fixed duty/pps:
        #   - below the AP's relay ceiling (slow/hard-MAC): echoes rise with inject, so it climbs to
        #     all-gas (target outgrows the card's burst -> the burst can't finish -> no sleep).
        #   - ceiling-limited (fast cards): echoes drop past the knee, so it snaps back to the peak.
        # Window is 2s (> the AP's ~1-2s relay lag) so a measurement isn't smeared across the burst.
        window = 2.0
        grow = 1.25
        target = 100.0                      # kickstart injects/window
        best_echo, best_target = 0.0, target
        ewma, misses, since_probe = -1.0, 0, 0
        climbing = True
        print(f"\n[Phase C] GENERATE {args.generate:g}s: strategy=adaptive climb "
              f"(window={window:g}s, grow={grow:g})")
        start = time.time()
        end = start + args.generate
        tap.reset()
        total_inj = 0
        last_report = start
        while time.time() < end:
            cyc = time.time()
            echoes_before = tap.echoes
            sent = 0
            while sent < int(target) and time.time() < end and (time.time() - cyc) < 2 * window:
                await iface.send_no_wait(frame)
                sent += 1
                total_inj += 1
            if tap.deauth_to_us:
                print("  deauthed -> re-associating")
                tap.deauth_to_us = 0
                await fake_auth(iface, tap, bssid, our_mac, args.essid, args.active_monitor)
            rest = window - (time.time() - cyc)
            if rest > 0:
                await asyncio.sleep(rest)               # listen: RX airtime for the echoes
            wdur = max(1e-3, time.time() - cyc)
            echo_rate = (tap.echoes - echoes_before) / wdur
            ewma = echo_rate if ewma < 0 else 0.5 * echo_rate + 0.5 * ewma
            since_probe += 1
            if climbing:
                if ewma > best_echo * 1.03:            # higher helped -> remember + keep climbing
                    best_echo, best_target, misses = ewma, target, 0
                    target *= grow
                else:
                    misses += 1                        # no gain; tolerate one lagged window
                    if misses >= 2:
                        target, climbing, misses, since_probe = best_target, False, 0, 0
                    else:
                        target *= grow
            else:
                target = best_target                   # hold at the peak
                if since_probe >= 8:                   # re-probe the ceiling every ~16s
                    climbing, since_probe = True, 0
            target = max(20.0, min(target, best_target * 4 + 200))
            if time.time() - last_report >= 4.0:
                last_report = time.time()
                print(f"  [{'climb' if climbing else 'hold '}] target={target:.0f}/win "
                      f"echo={echo_rate:.0f}/s best={best_echo:.0f}@{best_target:.0f} "
                      f"usable_IVs={len(tap.echo_ivs)}")
        dur = time.time() - start
        print(f"  DONE(adaptive): injected={total_inj} ({total_inj / dur:.0f}/s)  "
              f"usable IVs={len(tap.echo_ivs)} ({len(tap.echo_ivs) / dur:.0f}/s)  "
              f"best={best_echo:.0f}/s @ target {best_target:.0f}/win")
        return 0
    if args.generate:
        duty = max(0.0, min(1.0, args.duty))
        print(f"\n[Phase C] GENERATE {args.generate:g}s: duty={duty:g} "
              f"(inject {duty * args.window:g}s / sleep {(1 - duty) * args.window:g}s per "
              f"{args.window:g}s window)")
        start = time.time()
        end = start + args.generate
        tap.reset()
        total_inj = 0
        last_report = start
        while time.time() < end:
            cyc = time.time()
            burst_end = cyc + duty * args.window
            while time.time() < burst_end and time.time() < end:
                await iface.send_no_wait(frame)
                total_inj += 1
                if args.delay:
                    await asyncio.sleep(args.delay)
            if tap.deauth_to_us:
                print("  deauthed -> re-associating")
                tap.deauth_to_us = 0
                await fake_auth(iface, tap, bssid, our_mac, args.essid, args.active_monitor)
            rest = args.window - (time.time() - cyc)
            if rest > 0:
                await asyncio.sleep(rest)
            if time.time() - last_report >= 3.0:
                last_report = time.time()
                print(f"  injected={total_inj} usable_IVs={len(tap.echo_ivs)}")
        dur = time.time() - start
        print(f"  DONE: duty={duty:g}  injected={total_inj} ({total_inj / dur:.0f}/s)  "
              f"usable IVs={len(tap.echo_ivs)} ({len(tap.echo_ivs) / dur:.0f}/s)")
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
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between injects within a burst (0 = all-gas)")
    ap.add_argument("--duty", type=float, default=1.0,
                    help="TX duty cycle in --generate: inject duty*window, sleep the rest (1.0 = AGNB)")
    ap.add_argument("--window", type=float, default=1.0,
                    help="duty-cycle / adaptive window length in seconds (default 1.0)")
    ap.add_argument("--strategy", choices=["duty", "adaptive"], default="duty",
                    help="--generate strategy: fixed --duty, or adaptive (pace to measured echo rate)")
    ap.add_argument("--headroom", type=float, default=1.2,
                    help="adaptive: next-window inject count = echo_rate * window * headroom")
    ap.add_argument("--own-mac", action="store_true",
                    help="fake-auth as the card's own MAC (FIXED_MAC cards only HW-ACK that)")
    ap.add_argument("--card", type=str, default="", help="adapter substring (default: first found)")
    args = ap.parse_args()

    parse_wep_key(args.pass_)   # validate early

    print("[*] Discovering interfaces...")
    ifaces = wlan_ifaces()
    iface = select_device(ifaces, args.card)
    if iface is None:
        await wlan_close(ifaces)
        return 1

    def _progress(pct, msg):
        print(f"  [{int(pct * 100):3d}%] {msg}")

    print(f"[*] Bringing up {iface.description}...")
    try:
        if not await iface.connect(progress_cb=_progress):
            await wlan_close(ifaces)
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"[-] bring-up failed: {e}")
        await wlan_close(ifaces)
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
        await wlan_close(ifaces)
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        raise SystemExit(130)
