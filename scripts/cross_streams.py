"""Cross the streams: two cards, one deduplicated 802.11 RX stream.

Two monitor-mode radios tuned to the same channel hear (mostly) the same air. This loads both
at once, merges their parsed-frame streams, and drops the cross-card duplicates so each on-air
transmission surfaces exactly once. What one card's antenna misses the other often catches, so
the merged stream is strictly richer than either card alone (the diversity payoff is printed at
exit).

The dedup key is FCS-independent and driver-independent: frame-control + addr1/2/3 + seq_ctrl
(the MPDU header, bytes [0:2] + [4:24]). The same transmission heard by both cards carries
identical addresses and sequence number, so it collapses to one. A retransmission flips the
Retry bit inside frame-control, and a fresh frame from the same station steps seq_ctrl, so
neither is wrongly merged. Keys live in a short time window (--window) so a much-later frame that
happens to reuse a sequence number is never suppressed.

Pick the two cards by case-insensitive substring (--card / --sniffer); the default is the first
two present. Both tune to --channel (dedup only fires when both hear the same air, so they share
one channel). Output prefixes: [+] ok  [*] step  [#] a number  [-] a problem.
"""
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows console is cp1252
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager

LABELS = "AB"   # per-source tag in the live stream (two cards)


class StreamMerger:
    """Joins N parsed-frame sources into one deduplicated stream.

    ``submit`` returns True when a frame is novel (emit it) and False when it is a copy already
    delivered by another source within ``window``. Along the way it tallies the payoff: how many
    unique transmissions were heard by BOTH cards versus only one, which is the coverage a single
    card would have lost."""

    def __init__(self, sources: list[str], window: float = 0.3):
        self.sources = sources
        self.window = window
        self._seen: dict[bytes, list] = {}          # key -> [first_ts, {src_idx}]
        self._last_evict = 0.0
        self.novel = 0                              # unique on-air transmissions emitted
        self.dup = 0                                # cross-card copies suppressed
        self.both = 0                               # unique frames heard by >= 2 cards
        self.rx = [0] * len(sources)                # frames each card delivered
        self.first = [0] * len(sources)             # frames each card was first to deliver
        self.only = [0] * len(sources)              # unique frames only this card heard (at evict)

    @staticmethod
    def key(raw: bytes) -> bytes:
        """FC + addr1/2/3 + seq_ctrl. A control frame short of a full header (should not reach
        here, the parser drops them) falls back to its whole buffer so it still dedups sanely."""
        raw = bytes(raw)
        if len(raw) >= 24:
            return raw[0:2] + raw[4:24]
        return raw

    def submit(self, src: int, raw: bytes, now: float) -> bool:
        self.rx[src] += 1
        k = self.key(raw)
        ent = self._seen.get(k)
        if ent is not None and now - ent[0] <= self.window:
            self.dup += 1
            if src not in ent[1]:
                ent[1].add(src)
                if len(ent[1]) == 2:
                    self.both += 1
            return False
        self._seen[k] = [now, {src}]
        self.novel += 1
        self.first[src] += 1
        return True

    def evict(self, now: float) -> None:
        """Retire keys older than the window, tallying the ones only a single card ever heard.
        Rate-limited to once per window so a busy channel does not rescan the dict every frame."""
        if now - self._last_evict < self.window:
            return
        self._last_evict = now
        dead = [k for k, ent in self._seen.items() if now - ent[0] > self.window]
        for k in dead:
            _, srcs = self._seen.pop(k)
            if len(srcs) == 1:
                self.only[next(iter(srcs))] += 1

    def flush(self) -> None:
        """Evict everything so ``only`` is final for the exit summary."""
        for _, srcs in self._seen.values():
            if len(srcs) == 1:
                self.only[next(iter(srcs))] += 1
        self._seen.clear()


def pick(ifaces, sub: str, exclude=None):
    """First interface whose description contains ``sub`` (case-insensitive), skipping ``exclude``."""
    for i in ifaces:
        if i is exclude:
            continue
        if not sub or sub.lower() in (i.description or "").lower():
            return i
    return None


def on_channel(iface, ch: int) -> bool:
    return ch in iface.driver.SUPPORTED_CHANNELS


def sink(merger: StreamMerger, src: int, pkt, quiet: bool) -> None:
    """Print one line for a novel frame: which card caught it first, then the frame."""
    if quiet:
        return
    ssid = getattr(pkt, "ssid", None)
    tail = f'  "{ssid}"' if ssid else ""
    print(f"  [{LABELS[src]}] {pkt.type:<11}{pkt.bssid}  {pkt.source}->{pkt.dest}  "
          f"{pkt.rssi:>4}dBm{tail}")


def dashboard(merger: StreamMerger, fps: float) -> None:
    a, b = merger.first[0], merger.first[1]
    print(f"[#] {merger.novel} novel (A:{a} B:{b}) | {merger.dup} dup suppressed | "
          f"both:{merger.both} onlyA:{merger.only[0]} onlyB:{merger.only[1]} | {fps:.0f} fps")


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)

    target = ""
    if a.beacons:
        if a.channel is None:
            print("[-] --beacons requires --channel (the AP's channel), so both cards camp there")
            return
        target = a.beacons.strip().lower()
        if len(target.split(":")) != 6:
            print(f"[-] --beacons wants a BSSID like aa:bb:cc:dd:ee:ff, got '{a.beacons}'")
            return
    channel = a.channel if a.channel is not None else 1

    ifaces = await WlanDeviceManager().refresh()
    if len(ifaces) < 2:
        print(f"[-] need two cards; found {len(ifaces)}")
        for i in ifaces:
            print(f"      {i.description}")
        return

    card = pick(ifaces, a.card)
    sniffer = pick(ifaces, a.sniffer, exclude=card)
    if card is None or sniffer is None or card is sniffer:
        print(f"[-] could not pick two distinct cards (card='{a.card}' sniffer='{a.sniffer}'). plugged in:")
        for i in ifaces:
            print(f"      {i.description}  (ch {i.driver.SUPPORTED_CHANNELS[:4]}...)")
        return
    both = [card, sniffer]
    if not all(on_channel(i, channel) for i in both):
        print(f"[-] both cards must support channel {channel}:")
        for i in both:
            print(f"      {i.description}  supports {i.driver.SUPPORTED_CHANNELS[:6]}...")
        return

    print(f"[+] stream A: {card.description}")
    print(f"[+] stream B: {sniffer.description}")

    print("[*] connecting both (sequential: two cards driving RF bring-up over USB at once can collide)...")
    a_ok = await card.connect()
    b_ok = await sniffer.connect()
    if not (a_ok and b_ok):
        print(f"[-] connect failed (A={a_ok} B={b_ok})")
        return
    await card.set_channel(channel)
    await sniffer.set_channel(channel)
    print(f"[*] both on channel {channel}, window {a.window*1000:.0f} ms. crossing the streams. Ctrl+C to stop.\n")

    merger = StreamMerger([card.description, sniffer.description], window=a.window)
    # A second merger fed only the target AP's beacons: same dedup, so its novel count is the AP's
    # true (deduplicated) beacon rate, while each card's rx count is that radio's own view.
    beacons = StreamMerger([card.description, sniffer.description], window=a.window) if target else None

    def make_cb(src: int):
        def cb(pkt) -> None:
            now = time.monotonic()
            novel = merger.submit(src, pkt.raw, now)
            if beacons is not None and pkt.type == "beacon" and pkt.bssid == target:
                if beacons.submit(src, pkt.raw, now) and not a.quiet:
                    print(f"  [{LABELS[src]}] beacon {pkt.bssid}  {pkt.rssi:>4}dBm")
            elif novel and not target:
                sink(merger, src, pkt, a.quiet)
        return cb

    card.register_rx_callback(make_cb(0))
    sniffer.register_rx_callback(make_cb(1))

    start = time.monotonic()
    last_novel = 0
    last_t = start
    deadline = start + a.duration if a.duration else float("inf")
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(a.stats_interval)
            now = time.monotonic()
            merger.evict(now)
            if beacons is not None:
                beacons.evict(now)
                el = max(now - start, 1e-6)
                print(f"[#] AP {target} beacons: merged {beacons.novel/el:.1f}/s "
                      f"(A:{beacons.rx[0]/el:.1f}/s B:{beacons.rx[1]/el:.1f}/s) | "
                      f"both:{beacons.both} onlyA:{beacons.only[0]} onlyB:{beacons.only[1]}")
            else:
                fps = (merger.novel - last_novel) / max(now - last_t, 1e-6)
                last_novel, last_t = merger.novel, now
                dashboard(merger, fps)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        el = max(time.monotonic() - start, 1e-6)
        merger.flush()
        a_only, b_only = merger.only[0], merger.only[1]
        print("\n[+] streams crossed. merged 802.11 RX:")
        print(f"[#] {merger.novel} unique on-air frames | {merger.dup} cross-card copies suppressed")
        print(f"[#]   heard by BOTH cards: {merger.both}")
        print(f"[#]   only A ({card.description}) caught: {a_only}")
        print(f"[#]   only B ({sniffer.description}) caught: {b_only}")
        if merger.novel:
            print(f"[+] card A alone would have missed {b_only}; card B alone would have missed "
                  f"{a_only}. Crossed, you saw {merger.novel}.")
        if beacons is not None:
            beacons.flush()
            print(f"\n[+] AP {target} beacons over {el:.1f}s:")
            print(f"[#]   card A saw {beacons.rx[0]} ({beacons.rx[0]/el:.2f}/s)")
            print(f"[#]   card B saw {beacons.rx[1]} ({beacons.rx[1]/el:.2f}/s)")
            print(f"[#]   merged unique: {beacons.novel} ({beacons.novel/el:.2f}/s)  <- the AP's true beacon rate")
            print(f"[#]   both heard: {beacons.both} | only A: {beacons.only[0]} | only B: {beacons.only[1]}")
            if not beacons.novel:
                print(f"[-]   no beacons from {target} seen. Right channel? Right BSSID?")
        await asyncio.gather(card.close(), sniffer.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Cross two cards into one deduplicated 802.11 RX stream")
    p.add_argument("--card", default="", help="substring of the first card (default: first present)")
    p.add_argument("--sniffer", default="", help="substring of the second card (default: first other)")
    p.add_argument("--channel", type=int, default=None, help="channel both cards tune to (default 1)")
    p.add_argument("--beacons", default="", metavar="BSSID",
                   help="measure beacons/second of this one AP (requires --channel)")
    p.add_argument("--window", type=float, default=0.3, help="dedup time window in seconds")
    p.add_argument("--stats-interval", type=float, default=2.0, help="seconds between dashboard lines")
    p.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = until Ctrl+C)")
    p.add_argument("--quiet", action="store_true", help="suppress the per-frame stream, show only the dashboard")
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
