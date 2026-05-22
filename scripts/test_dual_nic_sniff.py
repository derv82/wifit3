"""Dual-NIC sniff test: inject deauths on one card, sniff them on another.

Picks the RT3572 (injector under test) and any other supported card
(sniffer). Both go to ch1 in monitor mode. Sniffer registers an RX
callback that counts deauth frames matching the AP/client MACs we're
about to inject. Injector fires a burst. We then print:

  - How many deauths the SNIFFER saw on-air (definitive proof of RF
    emission)
  - How many TX_SUCCESS the INJECTOR reports

If sniffer sees N matching deauths and injector reports N TX_SUCCESS,
the RT3572 is emitting and the phone/AP is silently filtering.
If sniffer sees 0 but injector reports N TX_SUCCESS, the chip's
digital MAC is lying about TX_SUCCESS and the RF stage is dead.

Run with --debug for full driver logging.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager

AP_MAC = "aa:bb:cc:dd:ee:01"
CLIENT_MAC = "04:2e:c1:51:43:b8"
CHANNEL = 1
BURST_COUNT = 20


def mac_bytes(mac_str: str) -> bytes:
    return bytes(int(x, 16) for x in mac_str.split(":"))


def fmt_mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


class DeauthCounter:
    """Counts on-air frames seen by the sniffer.

    Tracks total frames + beacons (sanity check the sniffer is alive)
    + deauths matching our target AP/client.
    """

    def __init__(self):
        self.ap = mac_bytes(AP_MAC)
        self.client = mac_bytes(CLIENT_MAC)
        self.total = 0
        self.beacons = 0
        self.any_deauth = 0
        self.matched = 0
        self.sample_frames: list[bytes] = []

    def __call__(self, frame_bytes: bytes, rssi: int, ts: float) -> None:
        if len(frame_bytes) < 24:
            return
        self.total += 1
        fc0 = frame_bytes[0]
        # FC byte 0: bits[3:2] = type (00=mgmt), bits[7:4] = subtype.
        # Beacon = type=0 subtype=8 → 0x80.
        # Deauth = type=0 subtype=12 → 0xC0.
        if fc0 == 0x80:
            self.beacons += 1
            return
        if fc0 != 0xC0:
            return
        self.any_deauth += 1
        addr1 = frame_bytes[4:10]
        addr2 = frame_bytes[10:16]
        addr3 = frame_bytes[16:22]
        ap_to_client = addr1 == self.client and addr2 == self.ap and addr3 == self.ap
        client_to_ap = addr1 == self.ap and addr2 == self.client and addr3 == self.ap
        if ap_to_client or client_to_ap:
            self.matched += 1
            if len(self.sample_frames) < 3:
                self.sample_frames.append(frame_bytes[:26])


async def main(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("[*] Discovering interfaces...")
    manager = WlanDeviceManager()
    interfaces = await manager.refresh()
    if len(interfaces) < 2:
        print(f"[-] Need at least 2 interfaces, found {len(interfaces)}.")
        for iface in interfaces:
            print(f"    - {iface.name} ({iface.description})")
        return

    # Pick the RT3572 as injector; any other as sniffer.
    injector = None
    sniffer = None
    for iface in interfaces:
        desc = (iface.description or "").lower()
        is_rt3572 = "rt3572" in desc or "awus051nh" in desc
        if is_rt3572 and injector is None:
            injector = iface
        elif not is_rt3572 and sniffer is None:
            sniffer = iface
    if injector is None or sniffer is None:
        print("[-] Could not pick one RT3572 + one other card.")
        print("    Found:")
        for iface in interfaces:
            print(f"      - {iface.name} ({iface.description})")
        return

    print(f"[+] Injector:  {injector.name} ({injector.description})")
    print(f"[+] Sniffer:   {sniffer.name} ({sniffer.description})")

    print("[*] Connecting both...")
    inj_ok, snf_ok = await asyncio.gather(injector.connect(), sniffer.connect())
    if not inj_ok:
        print("[-] Injector connect failed.")
        return
    if not snf_ok:
        print("[-] Sniffer connect failed.")
        return

    print(f"[*] Tuning both to channel {CHANNEL}...")
    await asyncio.gather(
        injector.set_channel(CHANNEL),
        sniffer.set_channel(CHANNEL),
    )

    print("[*] Registering sniffer deauth counter...")
    counter = DeauthCounter()
    sniffer.register_rx_callback(counter)

    print(f"[*] Waiting 2s for sniffer to settle...")
    await asyncio.sleep(2)
    baseline = (counter.total, counter.beacons, counter.any_deauth, counter.matched)
    print(f"    Pre-burst sniffer: total={baseline[0]} beacons={baseline[1]} "
          f"any_deauth={baseline[2]} matched={baseline[3]}")
    print(f"    Pre-burst sniffer.access_points: {len(sniffer.access_points)} APs")
    if sniffer.access_points:
        for bssid, ap in list(sniffer.access_points.items())[:5]:
            ssid = getattr(ap, "essid", None) or "?"
            print(f"      {bssid}  {ssid}")
    else:
        print("    [!] Sniffer saw NO APs in 2s — RX may be broken/wrong-channel")

    print(f"[*] Firing {BURST_COUNT} deauth bursts from injector...")
    for i in range(BURST_COUNT):
        await injector.deauth(AP_MAC, CLIENT_MAC, burst_count=1)
        await asyncio.sleep(0.05)

    print("[*] Waiting 3s for in-flight frames...")
    await asyncio.sleep(3)

    delta_total = counter.total - baseline[0]
    delta_beacons = counter.beacons - baseline[1]
    delta_any = counter.any_deauth - baseline[2]
    delta_matched = counter.matched - baseline[3]

    print("")
    print("=" * 60)
    print(f"  Injected:                  {BURST_COUNT * 2} deauth frames")
    print(f"  Sniffer total frames:      {delta_total}")
    print(f"  Sniffer beacons:           {delta_beacons}  (RX-alive sanity)")
    print(f"  Sniffer any deauth:        {delta_any}")
    print(f"  Sniffer matched deauth:    {delta_matched}")
    print("=" * 60)
    if delta_total == 0:
        print("[!] Sniffer saw ZERO frames during burst — sniffer RX is broken.")
        print("    Test inconclusive. Make sure sniffer card is on ch1 and")
        print("    actually in monitor mode.")
        await asyncio.gather(injector.close(), sniffer.close())
        return
    if delta_beacons == 0:
        print("[!] Sniffer saw frames but no beacons — possibly wrong channel?")
    if delta_matched > 0:
        print("[+] RT3572 IS emitting on-air. The chip's TX is working.")
        print("    Phone/AP filtering is the remaining mystery — try a different")
        print("    target, or check for PMF/802.11w on the AP.")
        for i, frame in enumerate(counter.sample_frames):
            print(f"    sample[{i}]: {frame.hex()}")
    elif delta_any > 0:
        print("[?] Sniffer saw OTHER deauth frames but none matching our MACs.")
        print("    RT3572 might be transmitting but with mangled addresses.")
    else:
        print("[-] Sniffer saw ZERO matching deauths.")
        print("    Chip's TX_STA_FIFO reports success but no RF is on-air.")
        print("    Analog/PA stage is the culprit, not the digital MAC.")

    print("")
    print("[*] Closing interfaces...")
    await asyncio.gather(injector.close(), sniffer.close())
    print("[+] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main(debug=args.debug))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[-] Test failed: {e}")
        import traceback
        traceback.print_exc()
