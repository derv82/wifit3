"""AR9271 monitor RX-filter probe (live hardware).

Question: is the card actually promiscuous? We capture RX for a few seconds per
channel and categorize frames. **unicast-between-others > 0** proves PROM is
working (we see traffic not addressed to us). Then we re-send the EXACT kernel
monitor RX-filter write observed in the pcap (AR_RX_FILTER=0xC03F + 0x810c=0)
and re-measure — to tell "our write didn't land" from "the write isn't enough".

Run (card cold/warm both fine):  uv run python scripts/ar9271/test_rxfilter_probe.py
"""
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.append(str(Path(__file__).parent.parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.chips.ar9271.constants import WMI_REG_WRITE_CMDID  # noqa: E402

if TYPE_CHECKING:
    from wifit3.wlan.packet import Packet

# Exact kernel monitor RX-filter REG_WRITE payload [WIRE captures_ath9k_htc
# frame 3047]: (AR_RX_FILTER=0x803c → 0xC03F), (0x810c → 0).
KERNEL_RXFILTER_WRITE = bytes.fromhex("0000803c0000c03f0000810c00000000")


def categorize(parsed: "Packet", our_mac: str) -> str:
    dst = (parsed.dest or "").lower()
    src = (parsed.source or "").lower()
    if dst.startswith("ff:ff:ff"):
        return "bcast"
    try:
        if int(dst.split(":")[0], 16) & 1:   # I/G bit set → multicast
            return "mcast"
    except ValueError:
        pass
    if our_mac and (dst == our_mac or src == our_mac):
        return "to/from-us"
    return "UNICAST-OTHERS"   # the promiscuity tell


async def _measure(iface, our_mac, counts, samples, seconds):
    counts.clear()
    samples.clear()
    await asyncio.sleep(seconds)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] No interface found")
        return
    iface = ifaces[0]
    print(f"[+] {iface.name} ({iface.description})")
    if not await iface.connect():
        print("[-] connect failed")
        return

    our_mac = (getattr(iface.driver, "mac_address", "") or "").lower()
    print(f"[*] our MAC: {our_mac!r}")

    counts: Counter = Counter()
    samples: list = []

    def cb(parsed: "Packet"):
        cat = categorize(parsed, our_mac)
        counts[cat] += 1
        if cat == "UNICAST-OTHERS" and len(samples) < 5:
            samples.append(f"{parsed.source}->{parsed.dest} "
                           f"type={parsed.type} "
                           f"to_ds={parsed.to_ds} from_ds={parsed.from_ds}")

    # Replace the interface's parsed-frame callback with our categorizer.
    iface.driver.register_rx_callback(cb)

    print("\n=== PHASE A: our driver's filter (incl. _apply_monitor_rx_filter) ===")
    for ch in (1, 6, 11):
        await iface.set_channel(ch)
        counts.clear()
        samples.clear()
        await asyncio.sleep(7)
        print(f"  ch{ch}: {dict(counts)}")
        for s in samples:
            print(f"       sample: {s}")

    print("\n=== PHASE B: re-send EXACT kernel RX-filter write, stay on ch ===")
    await iface.driver.send_wmi_command(WMI_REG_WRITE_CMDID, KERNEL_RXFILTER_WRITE)
    counts.clear()
    samples.clear()
    await asyncio.sleep(7)
    print(f"  after exact kernel write: {dict(counts)}")
    for s in samples:
        print(f"       sample: {s}")

    await iface.close()
    print("[+] done")


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=90))
    except asyncio.TimeoutError:
        print("[-] overall timeout (90s)")
    except Exception as e:
        import traceback
        print(f"[-] error: {e}")
        traceback.print_exc()
