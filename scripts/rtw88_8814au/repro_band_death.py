"""Reproduce the 5G->2G band-transition RX death (user repro: filter CH44, then
CH1, repeat — 2G goes silent after a hop down from 5G; a 5G tune revives it).

Mimics the TUI's single-channel-filter dwell: tune 5G ch, dwell; tune 2G ch1,
dwell + count ch1 beacons; repeat. A dwell on ch1 with ZERO beacons == the death.

Run:  uv run scripts/rtw88_8814au/repro_band_death.py [cycles]
Env:  WIFIT3_NO_HOP_EXTRAS=1  -> driver skips the per-hop cck-sensitivity +
      DIG re-seed writes (the things the kernel does NOT do per hop), for A/B.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import libusb_package
import usb.core

from wifit3.chips.rtw88_8814au import constants as C
from wifit3.chips.rtw88_8814au import rx as rx8814
from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver
from wifit3.wlan.packet import WlanFrameParser


async def main(cycles: int) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backend = libusb_package.get_libusb1_backend()
    dev = None
    for did in RTL8814AUDriver.SUPPORTED_IDS:
        dev = usb.core.find(idVendor=did.vid, idProduct=did.pid, backend=backend)
        if dev:
            break
    if dev is None:
        print("No RTL8814AU found.")
        return 1

    driver = RTL8814AUDriver.from_usb_device(dev, None)
    counts = {"beacons": 0}

    def on_frame(parsed: dict) -> None:
        if parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON:
            counts["beacons"] += 1

    driver.register_rx_callback(on_frame)
    print("Connecting…")
    if not await driver.connect():
        print("connect() failed.")
        return 1
    print(f"Online MAC={driver.mac_address}. Flapping 5G(44) <-> 2G(1) x{cycles}\n")

    deaths = 0
    for i in range(cycles):
        # 5 GHz dwell (always works per the user) — drains, then 2 GHz dwell.
        await driver.set_channel(44)
        counts["beacons"] = 0
        await asyncio.sleep(2.5)
        ch44 = counts["beacons"]

        await driver.set_channel(1)
        # Reset PHY counters right after the tune so the readout reflects this
        # ch1 dwell only, then classify the layer if it dies.
        rx8814.reset_phy_counters(driver.transport)
        counts["beacons"] = 0
        await asyncio.sleep(3.0)
        ch1 = counts["beacons"]

        # Layer probe: CCA (RF heard energy?), CRC-OK (BB demodulated?) vs
        # beacons delivered (DMA/USB?). cca>0 & crc>0 & beacons=0 => DMA/USB wedge.
        t = driver.transport
        cck_ok = t.read32(C.REG_CRC_CCK) & 0xFFFF
        ofdm_ok = t.read32(C.REG_CRC_OFDM) & 0xFFFF
        ht_ok = t.read32(C.REG_CRC_HT) & 0xFFFF
        cca = (t.read32(C.REG_CCA_OFDM) >> 16) & 0xFFFF

        dead = ch1 == 0
        deaths += dead
        tag = ""
        if dead:
            if cck_ok + ofdm_ok + ht_ok > 0:
                tag = f"   <<< DEAD but BB decoded (cck_ok={cck_ok} ofdm_ok={ofdm_ok} ht_ok={ht_ok}) -> DMA/USB wedge"
            elif cca > 0:
                tag = f"   <<< DEAD, cca={cca} but 0 CRC-ok -> demod-fail"
            else:
                tag = "   <<< DEAD, cca=0 -> RF deaf on 2G"
        print(f"  cycle {i + 1:2d}: ch44 beacons={ch44:4d}  ->  ch1 beacons={ch1:4d}"
              f"  [cca={cca} cck_ok={cck_ok} ofdm_ok={ofdm_ok}]{tag}")

    print(f"\nDone. ch1-dead cycles: {deaths}/{cycles}")
    await driver.close()
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    raise SystemExit(asyncio.run(main(n)))
