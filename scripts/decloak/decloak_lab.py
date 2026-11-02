"""Send Probe Requests naming one SSID and report, per probe, whether the AP answered.

Wired the way a campaign is: the probing card is attached to a WlanArray, so the WlanSink
builds the AccessPoint and our own transmissions are filtered out of it. Per-probe answers
are read off that card's raw RX stream, which is where a campaign watches for its own
responses.

The sniffer is deliberately NOT in the array: the array dedupes identical frames across
cards, and the retransmitted copies of one probe are byte-identical, so pooling the
sniffer would erase exactly what it is there to count.

Live TX against a network you do not own is illegal. Point this only at your own AP.

    uv run python scripts/decloak/decloak_lab.py --bssid <bssid> --channel 6 --ssid <name>
    uv run python scripts/decloak/decloak_lab.py ... --probes 10 --active-monitor
    uv run python scripts/decloak/decloak_lab.py ... --probes 10 --sniffer-card 8822
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))

from dev import select_device
from wifit3.campaigns.auth_assoc import Association, AssocState, build_client_leaving
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.dot11 import mac_to_str, str_to_mac
from wifit3.dot11.mac import BROADCAST_MAC, random_client_mac
from wifit3.dot11.probe import probe_req
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray

PROBE_RESPONSE_WAIT_S = 0.15
BEACON_LISTEN_S = 2.0
AUTH_TIMEOUT_S = 0.2
ASSOC_TIMEOUT_S = 0.3

PROBE_REQ = 0x40
PROBE_RESP = 0x50
BEACON = 0x80
RETRY_BIT = 0x08


@dataclass
class Probe:
    """One probe: what the probing card saw, then what the sniffer saw on the air."""

    acks: int
    answers: int
    latency: Optional[float]
    name: Optional[str]
    copies: Optional[int] = None
    seqs: list[int] = field(default_factory=list)
    retries: Optional[int] = None
    ap_responses: Optional[int] = None
    acks_sniffed: Optional[int] = None


class ProberCounts:
    """Probing card's raw RX stream: the target's Probe Responses addressed to us. Without
    the Addr1 check this also counts the AP's replies to other clients, which on a live
    network is most of them."""

    def __init__(self, bssid: str, our_mac: bytes) -> None:
        self.bssid = bssid
        self.our_mac = our_mac
        self.answers: list[tuple[float, Optional[str]]] = []
        self.foreign_answers = 0
        self.answer_raw: Optional[bytes] = None

    def __call__(self, pkt) -> None:
        if pkt.type != "probe_resp" or (pkt.bssid or "").lower() != self.bssid:
            return
        if pkt.raw and len(pkt.raw) >= 10 and pkt.raw[4:10] == self.our_mac:
            self.answers.append((time.monotonic(), pkt.ssid))
            if self.answer_raw is None:
                self.answer_raw = pkt.raw
        else:
            self.foreign_answers += 1


class SnifferCounts:
    """Second card's raw RX stream: on-air copies of our probes keyed by sequence number,
    the target's Probe Responses to us, and the target's beacons. Keys on Addr2 so a
    broadcast-addressed probe is counted the same as a directed one."""

    def __init__(self, our_mac: bytes, bssid: bytes) -> None:
        self.our_mac = our_mac
        self.bssid = bssid
        self.beacons = 0
        self.copies: Counter[int] = Counter()
        self.retries = 0
        self.ap_responses = 0

    def __call__(self, pkt) -> None:
        raw = pkt.raw
        if not raw or len(raw) < 10:
            return
        if raw[0] == BEACON:
            self.beacons += 1
        elif len(raw) >= 24 and raw[0] == PROBE_REQ and raw[10:16] == self.our_mac:
            self.copies[int.from_bytes(raw[22:24], "little") >> 4] += 1
            if raw[1] & RETRY_BIT:
                self.retries += 1
        elif (len(raw) >= 24 and raw[0] == PROBE_RESP
                and raw[4:10] == self.our_mac and raw[10:16] == self.bssid):
            self.ap_responses += 1

    def snapshot(self) -> tuple[int, list[int], int, int]:
        return sum(self.copies.values()), sorted(self.copies), self.retries, self.ap_responses

    def reset(self) -> None:
        self.copies.clear()
        self.retries = self.ap_responses = 0


async def find_target(array: WlanArray, args) -> Optional[AccessPoint]:
    """Let the sink build the AccessPoint from beacons: the same object a campaign targets."""
    await asyncio.sleep(BEACON_LISTEN_S)
    ap = array.access_points.get(args.bssid.lower())
    if ap is None:
        print(f"[-] sink never saw {args.bssid.lower()} on channel {args.channel}")
        return None
    print(f"[*] sink has {ap.bssid}: ssid={ap.ssid!r} hidden={ap.is_hidden} "
          f"ch={ap.channel} beacons={ap.beacons} rsn={bool(ap.rsn_ie)}")
    return ap


async def sweep(iface, sniffer, prober: ProberCounts, sniffed: Optional[SnifferCounts],
                args, our_mac: bytes) -> list[Probe]:
    """One probe per PROBE_RESPONSE_WAIT_S slot. The slot is fixed, so probe spacing stays
    uniform whether or not the AP answered."""
    addressee = BROADCAST_MAC if args.broadcast else str_to_mac(args.bssid)
    frame = probe_req(addressee, our_mac, args.ssid, channel=args.channel)
    if args.verbose:
        print(f"[v] probe request we transmit ({len(frame)} bytes): {frame.hex()}")
    probes: list[Probe] = []
    for _ in range(args.probes):
        answers_before = len(prober.answers)
        acks_before = iface.acks_seen(our_mac)
        sniffed_acks_before = sniffer.acks_seen(our_mac) if sniffer is not None else 0
        if sniffed is not None:
            sniffed.reset()
        sent_at = time.monotonic()
        await iface.send_no_wait(frame)
        await asyncio.sleep(PROBE_RESPONSE_WAIT_S)
        answers = prober.answers[answers_before:]
        probe = Probe(
            acks=iface.acks_seen(our_mac) - acks_before,
            answers=len(answers),
            latency=(answers[0][0] - sent_at) if answers else None,
            name=answers[0][1] if answers else None,
        )
        if sniffed is not None:
            probe.copies, probe.seqs, probe.retries, probe.ap_responses = sniffed.snapshot()
        if sniffer is not None:
            probe.acks_sniffed = sniffer.acks_seen(our_mac) - sniffed_acks_before
        probes.append(probe)
    return probes


async def claim_names(iface, ap: AccessPoint, args, our_mac: bytes) -> int:
    """Authenticate once, then claim a fresh sentinel SSID and --ssid in turn, --probes
    times. An AP that accepts the sentinel accepts anything, so its verdict on --ssid
    means nothing; one that refuses the sentinel and accepts --ssid has named itself."""
    association = Association(iface, args.bssid, "", args.channel, our_mac=our_mac,
                              auth_timeout=AUTH_TIMEOUT_S, assoc_timeout=ASSOC_TIMEOUT_S,
                              assoc_trailer_ies=ap.rsn_ie or b"",
                              privacy=bool(ap.rsn_ie))
    association.start()
    try:
        if not await association.authenticate():
            print(f"[a] {args.bssid.lower()} will not authenticate us "
                  f"(auth_status={association.auth_status})")
            return 1
        print(f"[a] authenticated as {mac_to_str(our_mac)}, state={association.state.name}")
        verdicts: dict[str, list[Optional[int]]] = {"sentinel": [], "target": []}
        for _ in range(args.probes):
            for label, ssid in (("sentinel", f"wifit3-control-{os.urandom(8).hex()}"),
                                ("target", args.ssid)):
                if association.state is AssocState.UNAUTHENTICATED:
                    if not await association.authenticate():
                        print("[a] deauthenticated mid-sweep, stopping")
                        return 1
                verdicts[label].append(await association.associate_as(ssid))
        for label in ("sentinel", "target"):
            got = verdicts[label]
            accepted = sum(1 for s in got if s == 0)
            silent = sum(1 for s in got if s is None)
            codes = sorted({s for s in got if s not in (0, None)})
            name = args.ssid if label == "target" else "wifit3-control-<random>"
            print(f"[a] {label:8} {name!r}: accepted {accepted}/{len(got)}, "
                  f"refused with status {codes or '-'}, no answer {silent}")
        if any(s == 0 for s in verdicts["sentinel"]):
            print("[a] the AP accepts an invented SSID; association proves nothing here")
        elif any(s == 0 for s in verdicts["target"]):
            print(f"[a] CONFIRMED: refused the sentinel, accepted {args.ssid!r}")
        else:
            print(f"[a] refused both; {args.ssid!r} is not this AP's SSID by association")
    finally:
        association.stop()
        try:
            await iface.send_no_wait(build_client_leaving(str_to_mac(args.bssid), our_mac))
        except Exception as exc:
            print(f"[a] client-leaving cleanup failed: {exc}")
    return 0


def report(probes: list[Probe], prober: ProberCounts, sniffed: Optional[SnifferCounts],
           ap: AccessPoint, args) -> None:
    answered = [p for p in probes if p.answers]
    acks = sum(p.acks for p in probes)
    n = len(probes)
    print(f"[#] {n} probes, {ap.beacons} beacons from the target, "
          f"{prober.foreign_answers} Probe Response(s) to other clients ignored")
    print(f"[#] answered {len(answered)}/{n}, {acks} ACK(s) of our probes "
          f"({acks / max(n, 1):.1f} copies/probe)")
    print(f"[#] answered?  {''.join('+' if p.answers else '.' for p in probes)}")
    print(f"[#] acks/probe {''.join(str(min(p.acks, 9)) for p in probes)}")
    if sniffed is not None:
        copies = sum(p.copies or 0 for p in probes)
        seqs = {s for p in probes for s in p.seqs}
        print(f"[s] on air: {copies} copies of our {n} probes "
              f"({copies / max(n, 1):.1f}/probe), {sum(p.retries or 0 for p in probes)} "
              f"with the retry bit set, {len(seqs)} distinct sequence number(s)")
        print(f"[s] copies/probe {''.join(str(min(p.copies or 0, 9)) for p in probes)}")
        print(f"[s] AP responses on air {sum(p.ap_responses or 0 for p in probes)}, "
              f"ACKs to us {sum(p.acks_sniffed or 0 for p in probes)}, "
              f"{sniffed.beacons} beacons heard by the sniffer")
        print(f"[s] ap resp/probe {''.join(str(min(p.ap_responses or 0, 9)) for p in probes)}")
    if args.verbose and prober.answer_raw:
        print(f"[v] probe response to us ({len(prober.answer_raw)} bytes): "
              f"{prober.answer_raw.hex()}")
    if answered:
        names = sorted({p.name for p in answered if p.name})
        matched = [p for p in answered if p.name == args.ssid]
        ordered = sorted(p.latency for p in answered if p.latency is not None)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        print(f"[+] named {names}, {sum(p.answers for p in answered)} response frames total")
        print(f"[t] reply latency min +{ordered[0]:.3f}s  median "
              f"+{statistics.median(ordered):.3f}s  p95 +{p95:.3f}s  max "
              f"+{ordered[-1]:.3f}s   (n={len(ordered)})")
        if len(matched) == len(answered):
            print(f"[m] every answer carried the SSID we asked for: {args.ssid!r}")
        else:
            others = sorted({p.name for p in answered if p.name != args.ssid})
            print(f"[m] FALSE POSITIVE RISK: asked {args.ssid!r}, "
                  f"{len(answered) - len(matched)}/{len(answered)} answer(s) carried "
                  f"{others} instead")
    elif acks:
        print("[-] no answer, but the AP ACKed our probes: it heard us and declined")
    elif ap.beacons:
        print("[-] no answer and no ACK, though the target is audible")
    else:
        print("[-] no answer and no beacons: wrong channel, or out of range")
    print(f"[=] sink now reports ssid={ap.ssid!r} hidden={ap.is_hidden}")


async def run(array: WlanArray, iface, sniffer, args) -> int:
    if not await iface.set_channel(args.channel):
        print(f"[-] set_channel({args.channel}) failed")
        return 1
    ap = await find_target(array, args)
    if ap is None:
        return 1
    our_mac = random_client_mac()
    prober = None
    sniffed = None
    async with array.claim(iface):
        if args.active_monitor:
            assumed = await iface.set_fake_mac(our_mac, args.bssid)
            if assumed is None:
                print(f"[-] no active monitor on this card ({iface.driver.FAKE_MAC.value})")
                return 1
            our_mac = str_to_mac(assumed)
        array.register_forged_mac(our_mac)
        prober = ProberCounts(args.bssid.lower(), our_mac)
        iface.register_rx_callback(prober)
        if sniffer is not None:
            if not await sniffer.set_channel(args.channel):
                print(f"[-] sniffer set_channel({args.channel}) failed")
                return 1
            sniffed = SnifferCounts(our_mac, str_to_mac(args.bssid))
            sniffer.register_rx_callback(sniffed)
            # enable_rx_acks clears _our_tx_macs, so register our MAC after arming.
            await sniffer.enable_rx_acks()
            sniffer.driver._our_tx_macs.add(our_mac)
        method = "assoc" if args.assoc else ("broadcast" if args.broadcast else "directed")
        monitor = "active-monitor" if args.active_monitor else "plain-monitor"
        print(f"[*] CH{args.channel} {method} {monitor}: {args.probes}x {args.ssid!r} "
              f"at {args.bssid.lower()} as {mac_to_str(our_mac)}")
        if args.assoc and not args.active_monitor:
            print("[!] assoc without --active-monitor: the AP's Auth Resp goes unACKed, "
                  "so authentication will not complete")
        await iface.enable_rx_acks()
        try:
            if args.assoc:
                rc = await claim_names(iface, ap, args, our_mac)
                return rc
            probes = await sweep(iface, sniffer, prober, sniffed, args, our_mac)
        finally:
            await iface.disable_rx_acks()
            iface.unregister_rx_callback(prober)
            if args.active_monitor:
                await iface.clear_fake_mac()
            if sniffer is not None:
                await sniffer.disable_rx_acks()
                sniffer.unregister_rx_callback(sniffed)
            array.unregister_own_mac(our_mac)
    report(probes, prober, sniffed, ap, args)
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bssid", required=True, help="the AP whose answers we count")
    ap.add_argument("--channel", type=int, required=True, help="channel to tune to")
    ap.add_argument("--ssid", required=True, help="the name to ask for")
    ap.add_argument("--card", default="", help="adapter substring, e.g. 8812")
    ap.add_argument("--sniffer-card", default="",
                    help="second adapter to watch the air; optional")
    ap.add_argument("--probes", type=int, default=1, help="how many probes to send")
    ap.add_argument("--broadcast", action="store_true",
                    help="address the probe to ff:ff:ff:ff:ff:ff instead of --bssid")
    ap.add_argument("--assoc", action="store_true",
                    help="claim --ssid in an Association Request instead of probing")
    ap.add_argument("--active-monitor", action="store_true",
                    help="HW-ACK frames sent to our forged STA")
    ap.add_argument("--verbose", action="store_true",
                    help="hex-dump the probe request we send and the response we get")
    args = ap.parse_args()

    ifaces = wlan_ifaces()
    iface = select_device(ifaces, args.card)
    if iface is None:
        await wlan_close(ifaces)
        return 1
    sniffer = None
    if args.sniffer_card:
        sniffer = next((i for i in ifaces if i is not iface and args.sniffer_card.lower()
                        in f"{i.name} {i.description}".lower()), None)
        if sniffer is None:
            roster = ", ".join(i.description for i in ifaces if i is not iface)
            print(f"[-] no second card matches '{args.sniffer_card}'. "
                  f"Other cards: {roster or 'none'}")
            await wlan_close(ifaces)
            return 1
        print(f"[*] sniffer: {sniffer.description}")
    print(f"[*] probing with {iface.description}")
    array = WlanArray()
    try:
        # Sequentially, sniffer first: two cards driving USB bring-up at once collide.
        if sniffer is not None and not await sniffer.connect():
            print("[-] sniffer bring-up failed")
            await wlan_close(ifaces)
            return 1
        if not await iface.connect():
            await wlan_close(ifaces)
            return 1
    except Exception as exc:
        print(f"[-] bring-up failed: {exc}")
        await wlan_close(ifaces)
        return 1
    array.attach(iface)
    try:
        return await run(array, iface, sniffer, args)
    finally:
        await wlan_close(ifaces)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        raise SystemExit(130)
