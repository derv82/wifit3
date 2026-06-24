"""Throwaway: capture the GOOD (CRC-passing) frames — does the receiver actually work?

The error-accept tally showed ~99% CRC failure, but that count is inflated by
noise-triggered garbage. The hardware crc_err=0 bit marks genuinely-decoded frames.
This captures only those over a longer window and reports: count/sec, the DESC rate
split (all-low-rate => marginal EVM ceiling; spread => random loss), and how many
parse as real beacons with a real SSID (receiver fundamentally works) vs none.

SSID VALUES are never printed/committed -- only counts. Passive.
    uv run python scripts/rtl8821cu_dkms/good_capture.py [channel] [seconds]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chan, efuse, watchdog
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport
from wifit3.wlan.packet import WlanFrameParser

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF


def _open():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
    usb.util.claim_interface(dev, intf)
    return dev, intf


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print(f"[*] cold bring-up, tune ch{ch}, watchdog DIG enabled...")
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, ch)
        # run a few watchdog ticks so DIG settles the AGC like the real driver
        st = watchdog.WatchdogState(eeprom_thermal=info.eeprom_thermal,
                                    thermal_offset=efuse.thermal_offset(info))
        for _ in range(4):
            watchdog.tick(t, st)
            time.sleep(0.3)

        good = total_units = 0
        rate_good, ssids, subt = Counter(), set(), Counter()
        end = time.monotonic() + secs
        while time.monotonic() < end:
            buf = t.bulk_in()
            if not buf:
                continue
            off, n = 0, len(buf)
            while off + 24 <= n:
                d = query_rx_desc(buf[off:off + 24])
                if d.pkt_len <= 0:
                    break
                po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
                if off + po > n:
                    break
                total_units += 1
                if not (d.rpt_sel or d.crc_err or d.icv_err):
                    start = off + 24 + d.drvinfo_sz + d.shift_sz
                    mpdu = buf[start:start + d.pkt_len]
                    if len(mpdu) > 4:
                        mpdu = mpdu[:-4]            # strip FCS
                    good += 1
                    rate_good[d.data_rate] += 1
                    if mpdu:
                        subt[(mpdu[0] >> 4) & 0xF] += 1
                    p = WlanFrameParser.parse_80211_frame(mpdu, 0)
                    if p and p.get("ssid"):
                        ssids.add(p["ssid"])
                off += (po + 7) & ~7

        print(f"\n{good} GOOD frames in {secs:g}s ({good/secs:.1f}/s); "
              f"{total_units} total rx units")
        print(f"  GOOD DESC rate histogram: {dict(sorted(rate_good.items()))}  (<=3 CCK, 4=6M..11=54M)")
        print(f"  GOOD FC subtype histogram: {dict(sorted(subt.items()))}  (8=beacon,5=proberesp,4=probereq)")
        print(f"  distinct real SSIDs decoded: {len(ssids)}  (values withheld)")
        if ssids:
            print(f"  -> receiver WORKS (decodes real APs); issue is marginal yield, not dead demod")
        else:
            print(f"  -> zero real SSIDs decoded")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
