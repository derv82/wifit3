"""ACK-retry lab: how many times does a card retransmit an injected frame?

One card injects a client->AP deauth carrying a fake source MAC; the other plugged card sits in
monitor and counts how many on-air copies of each frame appear. The radio retransmits a unicast
frame until the destination ACKs, up to its retry limit, so a target that answers shows ~1 copy
and one that never answers piles up to the limit. The tool runs four scenarios -- active monitor
off/on, crossed with your --target and a hardcoded bogus address nothing will answer -- so you can
see whether active monitor changes anything.

Only the shared driver interface is used (inject_frame / enter_active_monitor / register_rx_callback),
so it works on any chipset. Pick the injector with --inject-card, a case-insensitive substring of the
adapter name (e.g. "rtl", "mt"); the first match injects, the next card sniffs.

Output prefixes: [+] found/ok   [*] step or scenario   [#] a measured number   [-] a problem.
Target BSSIDs are passed at runtime; nothing real is hardcoded.
"""
import argparse
import asyncio
import logging
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows console is cp1252
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager
from wifit3.wlan.interface import build_deauth
from wifit3.chips.driver import FakeMacSupport

FAKE_SRC = "02:11:22:33:44:55"        # fake unicast client MAC we inject as (Addr2); the AP's ACK
                                      # returns to it, so it must be unicast.
BOGUS = "02:00:00:00:00:01"           # unicast, never a real AP: nothing ACKs it, so the card
                                      # retransmits to its limit. Must be unicast (a group Addr1 is
                                      # never ACKed and never retried, which would kill the control).
SETTLE = 2.5                          # seconds to let retransmits finish + be sniffed
HEIGHT = 7                            # histogram height in rows (bump/drop to taste)


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


class CopyCounter:
    """Monitor RX sink: per HW sequence number, how many on-air copies of our injected frame the
    sniffer sees. Filters on both our source MAC (Addr2) AND the current target (Addr1), so a
    prior scenario's still-draining retransmits to a different target can't leak into this count.
    Beacons tracked only to confirm the sniffer is receiving."""

    def __init__(self) -> None:
        self.src: bytes | None = None
        self.target: bytes | None = None
        self.copies: dict[int, int] = {}
        self.beacons = 0

    def __call__(self, fb: bytes, rssi: int, ts: float) -> None:
        if fb and fb[0] == 0x80:
            self.beacons += 1
            return
        if self.src is None or self.target is None:
            return
        if len(fb) < 24 or fb[10:16] != self.src or fb[4:10] != self.target:
            return
        seq = int.from_bytes(fb[22:24], "little") >> 4
        self.copies[seq] = self.copies.get(seq, 0) + 1

    def arm(self, src: bytes, target: bytes) -> None:
        self.src, self.target = src, target
        self.copies = {}

    def per_seq(self) -> list[int]:
        return list(self.copies.values())


def draw_hist(per_seq: list[int]) -> None:
    """Vertical bar per copy-count (x-axis 1..max), height HEIGHT. Any nonzero column shows at
    least one row so the tail is never invisible; empty columns stay blank so gaps read clearly."""
    freq = Counter(per_seq)
    if not freq:
        print("    (sniffer saw none of our injected frames)")
        return
    hi, top = max(freq), max(freq.values())
    cols = list(range(1, hi + 1))
    bar = [math.ceil(freq.get(c, 0) / top * HEIGHT) if freq.get(c, 0) else 0 for c in cols]
    yw = len(str(top))
    for row in range(HEIGHT, 0, -1):
        yl = str(top).rjust(yw) if row == HEIGHT else ("1".rjust(yw) if row == 1 else " " * yw)
        print(f"   {yl} |" + "".join("#" if h >= row else " " for h in bar))
    print(f"   {' ' * yw} +" + "-" * len(cols))
    axis = [" "] * (len(cols) + 3)
    for t in sorted({1, hi} | set(range(5, hi + 1, 5))):
        for k, ch in enumerate(str(t)):
            if (t - 1) + k < len(axis):
                axis[(t - 1) + k] = ch
    print(f"   {' ' * yw}  " + "".join(axis))


async def run_scenario(injector, sniffer, counter: CopyCounter, *, active_monitor: bool,
                       target: bytes, is_bogus: bool, src: bytes, count: int, interval: float) -> None:
    drv = injector.driver
    if drv.FAKE_MAC == FakeMacSupport.SPOOFABLE:   # only spoofable cards have enter/exit_active_monitor
        await (drv.enter_active_monitor(src) if active_monitor else drv.exit_active_monitor())
    await asyncio.sleep(0.3)

    sniffer.driver._our_tx_macs.add(src)          # the sniffer's ACK tap counts ACKs whose RA==src
    frame = build_deauth(target, src, target, reason=7)   # a1=AP, a2=src, a3=AP
    counter.arm(src, target)
    acks0 = sniffer.driver.acks_seen(src)
    for _ in range(count):
        try:
            await drv.inject_frame(frame, use_no_ack=False)   # use_no_ack=False keeps HW retry on
        except Exception as e:                    # noqa: BLE001
            print(f"[-] inject failed: {e}")
            break
        await asyncio.sleep(interval)
    await asyncio.sleep(SETTLE)

    per_seq = counter.per_seq()
    acks = sniffer.driver.acks_seen(src) - acks0
    kind = "BOGUS" if is_bogus else "VALID"
    med = f"{statistics.median(per_seq):g}" if per_seq else "-"
    print(f"[*] {kind} target. inject_frame {count} -> {len(per_seq)} frames, "
          f"{sum(per_seq)} copies, {acks} ACKs back (median {med})")
    draw_hist(per_seq)
    print("[#] copies: " + " ".join(f"{c}x:{n}" for c, n in sorted(Counter(per_seq).items())))
    print()


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)

    ifaces = await WlanDeviceManager().refresh()
    if len(ifaces) < 2:
        print(f"[-] need two cards (injector + sniffer); found {len(ifaces)}")
        return
    want = a.inject_card.lower()
    injector = next((i for i in ifaces if want in (i.description or "").lower()
                     and a.channel in i.driver.SUPPORTED_CHANNELS), None)
    if injector is None:
        print(f"[-] no '{a.inject_card}' adapter supports channel {a.channel}. plugged in:")
        for i in ifaces:
            print(f"      {i.description}  (ch {i.driver.SUPPORTED_CHANNELS[:4]}...)")
        return
    sniffer = next((i for i in ifaces if i is not injector
                    and a.channel in i.driver.SUPPORTED_CHANNELS), None)
    if sniffer is None:
        print(f"[-] no second card supports channel {a.channel} to sniff with")
        return
    print(f"[+] injector: {injector.description}")
    print(f"[+] sniffer:  {sniffer.description}")

    print("[*] connecting both cards...")
    inj_ok, snf_ok = await asyncio.gather(injector.connect(), sniffer.connect())
    if not (inj_ok and snf_ok):
        print(f"[-] connect failed (injector={inj_ok} sniffer={snf_ok})")
        return
    await asyncio.gather(injector.set_channel(a.channel), sniffer.set_channel(a.channel))

    counter = CopyCounter()
    sniffer.register_rx_callback(counter)
    try:
        await sniffer.driver.enable_ack_detect()   # admit + tally the AP's ACKs on the sniffer
    except Exception as e:                          # noqa: BLE001
        print(f"[-] sniffer ACK detection unavailable: {e}")
    await asyncio.sleep(SETTLE)
    print("[+] sniffer alive" if counter.beacons else
          f"[-] sniffer sees nothing on channel {a.channel}; results are unreliable")
    print(f"[*] channel {a.channel}, valid={a.target}, bogus={BOGUS}")

    # Each pass = (header, src MAC we inject as, active monitor on/off). A spoofable card gets an
    # active-monitor ON pass; a card that can't spoof gets a pass injected as its OWN silicon MAC
    # instead -- the address whose ACK its retry logic should recognise, if it recognises any.
    fake = mac_bytes(a.src)
    passes = [(f"Active Monitor: OFF | src {a.src} (spoofed)", fake, False)]
    if injector.driver.FAKE_MAC == FakeMacSupport.SPOOFABLE:
        passes.append((f"Active Monitor: ON | src {a.src} (spoofed)", fake, True))
    else:
        silicon = injector.driver.mac_address
        if silicon:
            passes.append((f"Active Monitor: OFF | src {silicon} (Silicon)", mac_bytes(silicon), False))
        else:
            print(f"[-] {injector.description} can't spoof and its silicon MAC is unknown "
                  f"(warm boot?); replug to run the Silicon-src pass")
    scenarios = [(mac_bytes(a.target), False), (mac_bytes(BOGUS), True)]
    try:
        for header, src, am in passes:
            print(f"\n[*] {header}")
            for target, is_bogus in scenarios:
                await run_scenario(injector, sniffer, counter, active_monitor=am, target=target,
                                   is_bogus=is_bogus, src=src,
                                   count=a.count, interval=a.interval)
    finally:
        try:
            await injector.driver.exit_active_monitor()
        except Exception:                         # noqa: BLE001
            pass
        await asyncio.gather(injector.close(), sniffer.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ACK-retry lab: retransmit histogram, active monitor off vs on")
    p.add_argument("--target", required=True, help="AP BSSID to inject at, e.g. the valid-AP case")
    p.add_argument("--inject-card", default="", help="substring of the injector adapter name, e.g. rtl or mt")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--src", default=FAKE_SRC, help="fake client MAC we inject as (Addr2)")
    p.add_argument("--count", type=int, default=100, help="inject_frame() calls per scenario")
    p.add_argument("--interval", type=float, default=0.03)
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
