"""RX auto-ACK lab: does a card's hardware ACK frames addressed to it?

A card that auto-ACKs answers any unicast frame sent to a MAC it owns with a link-layer ACK.
This tests it directly, no AP needed:

  under test:  enters active monitor for a spoofed MAC (if it can), so its hardware should
               auto-ACK frames addressed to that MAC. We also probe its own silicon MAC.
  prober:      injects unicast frames addressed to that MAC, sourced from a fixed probe MAC S,
               and counts the ACKs that come back to S with its own ACK tap.

If the card under test auto-ACKs, an ACK addressed to S appears for (nearly) every injected
frame; if not, ~none. Controls: active monitor OFF, and a bogus MAC nobody owns.

Only the shared driver interface is used, so it runs on any chipset. Pick the card under test
with --test-card (case-insensitive substring); the prober is the first other card that supports
the channel (or set --prober-card).

Output prefixes: [+] found/ok   [*] step   [#] a measured number   [-] a problem.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                                 # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager
from wifit3.dot11 import build_deauth

PROBE_SRC = "02:b0:b0:00:00:01"   # the prober injects as this; the DUT's ACK comes back to it
SPOOF = "02:ac:ac:00:00:01"       # the spoofed MAC the DUT active-monitors
BOGUS = "02:00:00:00:00:99"       # unicast, nobody owns it: control, expect ~0 ACKs
SETTLE = 2.0


def mac_bytes(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def on_channel(iface, ch: int) -> bool:
    return ch in iface.driver.SUPPORTED_CHANNELS


async def probe(prober, dst: bytes, count: int, interval: float) -> int:
    """Inject ``count`` unicast frames addressed to ``dst`` (sourced from PROBE_SRC); return the
    number of ACKs the prober's tap saw come back to PROBE_SRC (i.e. the DUT's auto-ACKs)."""
    src = mac_bytes(PROBE_SRC)
    frame = build_deauth(dst, src, dst, reason=7)   # a1=dst (the DUT), a2=PROBE_SRC, a3=dst
    a0 = prober.driver.acks_seen(src)
    for _ in range(count):
        try:
            await prober.driver.inject_frame(frame)   # HW ACK-retry stops on the DUT's auto-ACK
        except Exception as e:                       # noqa: BLE001
            print(f"[-] inject failed: {e}")
            break
        await asyncio.sleep(interval)
    await asyncio.sleep(SETTLE)
    return prober.driver.acks_seen(src) - a0


async def main(a) -> None:
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.ERROR)
    ifaces = await WlanDeviceManager().refresh()

    want = a.test_card.lower()
    dut = next((i for i in ifaces if want in (i.description or "").lower()
                and on_channel(i, a.channel)), None)
    if a.prober_card:
        prober = next((i for i in ifaces if i is not dut and a.prober_card.lower() in
                       (i.description or "").lower() and on_channel(i, a.channel)), None)
    else:
        prober = next((i for i in ifaces if i is not dut and on_channel(i, a.channel)), None) if dut else None
    if dut is None or prober is None:
        print(f"[-] need a '{a.test_card}' card + a prober, both on channel {a.channel}. plugged in:")
        for i in ifaces:
            print(f"      {i.description}  (ch {i.driver.SUPPORTED_CHANNELS[:4]}...)")
        return
    print(f"[+] under test: {dut.description}")
    print(f"[+] prober:     {prober.description}")

    print("[*] connecting both...")
    d_ok, p_ok = await asyncio.gather(dut.connect(), prober.connect())
    if not (d_ok and p_ok):
        print(f"[-] connect failed (dut={d_ok} prober={p_ok})")
        return
    await asyncio.gather(dut.set_channel(a.channel), prober.set_channel(a.channel))
    try:
        await prober.driver.enable_rx_acks()
    except Exception as e:                            # noqa: BLE001
        print(f"[-] prober ACK tap unavailable: {e}")
    prober.driver._our_tx_macs.add(mac_bytes(PROBE_SRC))
    print(f"[*] channel {a.channel}, {a.count} frames per probe\n")

    # 1. spoofed MAC via active monitor (if the card can do it).
    spoof = mac_bytes(SPOOF)
    can_am = True
    try:
        await dut.driver.enter_active_monitor(spoof)
    except Exception as e:                            # noqa: BLE001
        can_am = False
        print(f"[*] {dut.description}: active monitor unavailable ({e})")
    on = off = None
    if can_am:
        on = await probe(prober, spoof, a.count, a.interval)
        try:
            await dut.driver.exit_active_monitor()
        except Exception:                             # noqa: BLE001
            pass
        off = await probe(prober, spoof, a.count, a.interval)
        print(f"[#] spoofed MAC {SPOOF}: active monitor ON -> {on}/{a.count} ACKed, "
              f"OFF -> {off}/{a.count} (control)")

    # 2. the card's own silicon MAC (with active monitor off / restored).
    try:
        await dut.driver.exit_active_monitor()
    except Exception:                                 # noqa: BLE001
        pass
    silicon = dut.driver.mac_address
    sil = None
    if silicon:
        sil = await probe(prober, mac_bytes(silicon), a.count, a.interval)
        print(f"[#] silicon MAC {silicon}: {sil}/{a.count} ACKed")
    else:
        print("[#] silicon MAC unknown (warm boot?); replug to test it")

    # 3. bogus control.
    bog = await probe(prober, mac_bytes(BOGUS), a.count, a.interval)
    print(f"[#] bogus MAC {BOGUS} (control): {bog}/{a.count} ACKed")

    margin = max(a.count // 4, 5)
    if can_am and on is not None and on > max(off or 0, bog) + margin:
        verdict = "auto-ACKs a SPOOFED MAC via active monitor"
    elif sil is not None and sil > bog + margin:
        verdict = "auto-ACKs its OWN silicon MAC only (not a spoofed one)"
    else:
        verdict = "does NOT auto-ACK"
    print(f"\n[+] {dut.description}: {verdict}")

    try:
        await dut.driver.exit_active_monitor()
    except Exception:                                 # noqa: BLE001
        pass
    await asyncio.gather(dut.close(), prober.close())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="RX auto-ACK lab: does a card ACK frames sent to it?")
    p.add_argument("--test-card", required=True, help="substring of the card under test, e.g. 8822 or mt")
    p.add_argument("--prober-card", default="", help="substring of the prober card (default: first other on channel)")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--interval", type=float, default=0.02)
    p.add_argument("--debug", action="store_true")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
