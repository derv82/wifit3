"""Does a hidden AP leak its SSID? One bench, asking the question four ways.

Answers, against YOUR OWN hidden AP, before any campaign code exists:

  1. Does a directed Probe Request carrying the CORRECT SSID get a Probe Response?
     If not, that is why guess-by-probe never worked and the suffix campaign is dead.
  2. Does Auth+Assoc carrying the CORRECT SSID get accepted while a WRONG SSID is
     rejected? If so, the association oracle is real and worth building.
  3. Does a RANDOM SSID also get accepted? If so the AP takes anything and the
     oracle is worthless against it.
  4. Is a refusal global or per-MAC? --reuse-mac holds one STA identity across every
     attempt; the default uses a fresh random MAC per attempt.

Follows the ACKed-exchange order from docs/ACKS.md: set_fake_mac() so the chip
auto-ACKs the AP's unicast reply (else the AP abandons the session), then
enable_rx_acks() to tally whether the AP ACKed us. Every row reports that tally, so a
silent Assoc Response can be told apart from a frame the AP never even ACKed.

The RSN IE and the privacy bit are copied from the target's own beacon, so a rejection
means the SSID was wrong rather than the capabilities not matching.

Live TX against a network you do not own is illegal. Point this only at your own AP.

    uv run python scripts/decloak/assoc_oracle_lab.py \
        --bssid 96:83:c4:8c:3f:78 --channel 11 --ssid GL-MT3000-f76-Guest
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.dot11.auth_assoc import auth_req, assoc_req, status_description
from wifit3.dot11.probe import probe_req
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket


def _mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _macs(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _rand_mac() -> bytes:
    return bytes([0x02]) + os.urandom(5)


class Tap:
    """Raw per-card RX, filtered to one BSSID and the STA identity currently armed."""

    def __init__(self, bssid: bytes):
        self.bssid_str = _macs(bssid)
        self.our_mac = b""
        self.beacon = None
        self.beacons = 0
        self.any_beacons = 0
        self.reset()

    def reset(self) -> None:
        self.probe_resp_ssids: list = []
        self.auth_status = None
        self.assoc_status = None
        self.auth_seen = 0
        self.assoc_seen = 0
        self.deauth_seen = 0

    def __call__(self, pkt) -> None:
        raw = pkt.raw
        if not raw or len(raw) < 24:
            return
        if pkt.type == "beacon":
            self.any_beacons += 1
            if (pkt.bssid or "").lower() == self.bssid_str:
                self.beacons += 1
                if self.beacon is None:
                    self.beacon = pkt
            return
        if (pkt.bssid or "").lower() != self.bssid_str:
            return
        if pkt.type == "probe_resp":
            self.probe_resp_ssids.append(pkt.ssid)
            return
        if raw[4:10] != self.our_mac:
            return
        if isinstance(pkt, AssocRespPacket):
            self.assoc_seen += 1
            if self.assoc_status is None:
                self.assoc_status = pkt.status
        elif isinstance(pkt, AuthPacket):
            self.auth_seen += 1
            if self.auth_status is None:
                self.auth_status = pkt.status
        elif isinstance(pkt, DeauthPacket):
            self.deauth_seen += 1


async def passive(iface, tap: Tap, channel: int, secs: float) -> bool:
    print(f"\n[A] passive {secs:g}s on CH{channel}")
    if not await iface.set_channel(channel):
        print(f"  [-] set_channel({channel}) failed")
        return False
    await asyncio.sleep(secs)
    print(f"  beacons, any AP   : {tap.any_beacons}")
    print(f"  beacons, target   : {tap.beacons}")
    if not tap.any_beacons:
        print("  [-] no beacons at all: wrong channel, or the radio is not listening")
        return False
    if not tap.beacons:
        print("  [-] the target BSSID never beaconed; check --bssid / --channel")
        return False
    rsn = getattr(tap.beacon, "rsn_ie_raw", None)
    print(f"  target advertises : {tap.beacon.ssid!r}")
    print(f"  RSN IE in beacon  : {len(rsn) if rsn else 0} bytes")
    return True


async def arm_sta(iface, bssid: bytes, want: bytes) -> bytes | None:
    """Enter active monitor as ``want`` so the chip auto-ACKs the AP's reply to us.
    Returns the MAC the card will actually ACK as, or None when it cannot."""
    try:
        armed = await iface.set_fake_mac(want, bssid)
    except Exception as exc:
        print(f"  [-] set_fake_mac failed: {exc}")
        return None
    return _mac(armed) if armed else None


async def probe_for(iface, tap: Tap, bssid: bytes, our_mac: bytes, ssid: str,
                    channel: int, wait: float, tries: int = 3) -> bool:
    """Directed Probe Requests for ``ssid``, one at a time, stopping at the first answer."""
    tap.our_mac = our_mac
    tap.reset()
    for _ in range(tries):
        await iface.send_no_wait(probe_req(bssid, our_mac, ssid, channel=channel))
        deadline = time.monotonic() + wait / tries
        while time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            if tap.probe_resp_ssids:
                return True
    return False


async def associate_with(iface, tap: Tap, bssid: bytes, our_mac: bytes, ssid: str,
                         channel: int, rsn: bytes, wait: float) -> tuple:
    """Auth then Assoc claiming ``ssid``. Returns (auth, assoc, deauths, acks_to_us)."""
    tap.our_mac = our_mac
    tap.reset()
    acks0 = iface.acks_seen(our_mac)
    await iface.send_no_wait(auth_req(bssid, our_mac))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and tap.auth_status is None:
        await asyncio.sleep(0.02)
    await iface.send_no_wait(assoc_req(bssid, our_mac, ssid, rsn,
                                       channel=channel, privacy=bool(rsn)))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and tap.assoc_status is None:
        await asyncio.sleep(0.02)
    return (tap.auth_status, tap.assoc_status, tap.deauth_seen,
            iface.acks_seen(our_mac) - acks0)


def _row(label: str, result: tuple) -> str:
    auth, assoc, deauths, acks = result
    a = "silent" if auth is None else f"{auth} {status_description(auth)}"
    s = "silent" if assoc is None else f"{assoc} {status_description(assoc)}"
    return (f"  {label:<16} auth={a:<28} assoc={s:<28} "
            f"deauth={deauths} ap_acked_us={acks}")


async def attempt(iface, tap: Tap, bssid: bytes, mac: bytes, ssid: str,
                  channel: int, rsn: bytes, wait: float) -> tuple:
    """Arm the STA identity, then run one Auth+Assoc attempt as it."""
    armed = await arm_sta(iface, bssid, mac)
    if armed is None:
        return (None, None, 0, 0)
    return await associate_with(iface, tap, bssid, armed, ssid, channel, rsn, wait)


async def run(iface, args) -> int:
    bssid = _mac(args.bssid)
    tap = Tap(bssid)
    iface.register_rx_callback(tap)
    if not await passive(iface, tap, args.channel, args.passive):
        return 3
    rsn = getattr(tap.beacon, "rsn_ie_raw", None) or b""

    if args.own_mac:
        if not iface.mac_address:
            print("[-] --own-mac: the card did not report a MAC")
            return 1
        held = _mac(iface.mac_address)
    else:
        held = _rand_mac()

    def sta() -> bytes:
        return held if (args.reuse_mac or args.own_mac) else _rand_mac()

    try:
        await iface.enable_rx_acks()
    except Exception as exc:
        print(f"[-] ACK tally unavailable, ap_acked_us will read 0: {exc}")

    probe_mac = await arm_sta(iface, bssid, sta())
    if probe_mac is None:
        print("[-] could not enter active monitor; the AP's replies will go unACKed "
              "and the result would be meaningless. Stopping.")
        return 1
    print(f"  active monitor as : {_macs(probe_mac)}")

    wrong = args.ssid + "-WRONG"
    control = "wifit3-" + os.urandom(6).hex()

    print("\n[B] directed Probe Request: does probe-based decloak work at all?")
    answered_right = await probe_for(iface, tap, bssid, probe_mac, args.ssid,
                                     args.channel, args.wait)
    print(f"  correct SSID -> answered={answered_right} ssids={tap.probe_resp_ssids}")
    answered_wrong = await probe_for(iface, tap, bssid, probe_mac, wrong,
                                     args.channel, args.wait)
    print(f"  wrong SSID   -> answered={answered_wrong} ssids={tap.probe_resp_ssids}")

    identity = "one held MAC" if (args.reuse_mac or args.own_mac) else "fresh MAC each"
    print(f"\n[C] Auth+Assoc oracle ({identity})")
    right = await attempt(iface, tap, bssid, sta(), args.ssid, args.channel, rsn, args.wait)
    print(_row("correct SSID", right))
    bad = await attempt(iface, tap, bssid, sta(), wrong, args.channel, rsn, args.wait)
    print(_row("wrong SSID", bad))
    ctl = await attempt(iface, tap, bssid, sta(), control, args.channel, rsn, args.wait)
    print(_row("random control", ctl))

    print("\n[D] verdict")
    if answered_right:
        print("  PROBE: works here. A correct directed probe is answered -> suffix")
        print("         guessing is viable against this AP.")
    else:
        print("  PROBE: dead here. The correct SSID drew no Probe Response, which is")
        print("         why guess-by-probe never worked.")
    accepted = right[1] == 0
    refused = [r for r in (bad, ctl) if r[1] != 0]
    if accepted and ctl[1] == 0:
        print("  ORACLE: useless here. The AP accepts any SSID.")
    elif accepted and len(refused) == 2:
        silent = all(r[1] is None for r in refused)
        how = "silence" if silent else "a rejection status"
        print(f"  ORACLE: REAL. Correct accepted; wrong and random answered with {how}.")
        if silent:
            print("          NOTE: the discriminator is accept-vs-SILENCE. Any oracle that")
            print("          demands an explicit reject status calls this AP inconclusive.")
        if not (right[3] or bad[3]):
            print("          Caveat: the AP never ACKed us, so treat the run as suspect.")
    elif not accepted and right[1] is None:
        acked = right[3] or bad[3]
        print("  ORACLE: inconclusive, the CORRECT SSID drew no Assoc Response either.")
        print(f"          The AP {'did' if acked else 'never'} ACK our frames, so "
              f"{'it is ignoring the Assoc' if acked else 'the TX is not reaching it'}.")
    else:
        print("  ORACLE: mixed. Read the rows above before concluding anything.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bssid", required=True, help="the hidden AP's BSSID")
    ap.add_argument("--channel", type=int, required=True, help="channel to tune to")
    ap.add_argument("--ssid", required=True, help="the hidden AP's real SSID (you own it)")
    ap.add_argument("--passive", type=float, default=6.0, help="passive RX seconds")
    ap.add_argument("--wait", type=float, default=1.5, help="seconds to wait per response")
    ap.add_argument("--reuse-mac", action="store_true",
                    help="hold one STA identity for every attempt (detects per-MAC lockout)")
    ap.add_argument("--own-mac", action="store_true",
                    help="use the card's own MAC (FIXED_MAC cards only ACK that address)")
    ap.add_argument("--card", type=str, default="", help="adapter substring, e.g. 8188")
    args = ap.parse_args()

    print("[*] Discovering interfaces...")
    ifaces = wlan_ifaces()
    iface = select_device(ifaces, args.card)
    if iface is None:
        await wlan_close(ifaces)
        return 1
    print(f"[*] Bringing up {iface.description}...")
    try:
        if not await iface.connect(progress_cb=lambda p, m: print(f"  [{int(p * 100):3d}%] {m}")):
            await wlan_close(ifaces)
            return 1
    except Exception as exc:
        print(f"[-] bring-up failed: {exc}")
        await wlan_close(ifaces)
        return 1
    print(f"[*] card MAC: {iface.mac_address}  FAKE_MAC={iface.driver.FAKE_MAC}")
    try:
        return await run(iface, args)
    finally:
        try:
            await iface.clear_fake_mac()
            await iface.disable_rx_acks()
        except Exception:
            pass
        await wlan_close(ifaces)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        raise SystemExit(130)
