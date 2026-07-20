"""RTL8822BU CCK-PWDB vs IGI probe — is the 2.4 GHz saturation/weakness gain-driven?

Pin OFDM IGI (0xc50/0xe50) to a series of values on a 2.4 GHz channel and, for
each, capture the page-0 (CCK) PWDB of every beacon. If a near AP's CCK PWDB
rails at ~251 regardless of IGI, the saturation is in the CCK front-end (AGC
table / missing CCK-PD / calibration), not the OFDM gain DIG touches. If far
APs' PWDB tracks IGI, there is a 2.4 gain lever.

Usage:
    .venv/Scripts/python.exe scripts/rtl8822bu/diag_cck_igi.py --channel 1
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8822bu.chan import set_channel_2g_20mhz, set_channel_5g_20mhz
from wifit3.chips.rtl8822bu.constants import REG_SYS_CFG1, USB_IDS_8822BU
from wifit3.chips.rtl8822bu.dynamic import DIG_IGI_MASK, REG_DIG_PATH
from wifit3.chips.rtl8822bu.firmware import download_firmware, download_firmware_validate, load_firmware_blob
from wifit3.chips.rtl8822bu.mac import cut_mask_from_sys_cfg1, is_chip_warm, mac_init_for_rx, mac_power_on
from wifit3.chips.rtl8822bu.phy import EfuseDefaults, phy_set_param
from wifit3.chips.rtl8822bu.rx import RX_PKT_DESC_SZ, parse_rx_pkt_desc, probe_endpoints, read_rx_burst
from wifit3.chips.rtl8822bu.transport import RTL8822BUTransport
from wifit3.dot11.parser import WlanFrameParser

IGI_LIST = [0x1C, 0x20, 0x24, 0x2A, 0x30, 0x3A, 0x4A, 0x60, 0x7F]
DWELL_S = 2.5


def ssid_of(mpdu: bytes) -> str:
    if len(mpdu) < 38 or mpdu[36] != 0:
        return ""
    n = mpdu[37]
    if 38 + n > len(mpdu):
        return ""
    try:
        return mpdu[38:38 + n].decode("utf-8")
    except UnicodeDecodeError:
        return mpdu[38:38 + n].hex()


def open_device():
    backend = libusb_package.get_libusb1_backend()
    for vid, pid, desc in USB_IDS_8822BU:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is not None:
            print(f"  Found {vid:04x}:{pid:04x}  {desc}")
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (NotImplementedError, usb.core.USBError):
                pass
            dev.set_configuration()
            usb.util.claim_interface(dev, 0)
            return dev
    print("No RTL8822BU found (close the UI if it's holding the device).")
    sys.exit(1)


def bring_up(dev, transport, ch):
    if is_chip_warm(transport):
        print("  Chip WARM — riding existing init.")
    else:
        print("  Chip COLD — full bring-up...")
        cut_mask = cut_mask_from_sys_cfg1(transport.read32(REG_SYS_CFG1))
        mac_power_on(transport, cut_mask=cut_mask)
        download_firmware(dev, transport, load_firmware_blob())
        ok, _ = download_firmware_validate(transport)
        if not ok:
            print("  FW validate FAILED"); sys.exit(1)
        phy_set_param(transport, EfuseDefaults())
        mac_init_for_rx(transport)
    (set_channel_2g_20mhz if ch <= 14 else set_channel_5g_20mhz)(transport, ch)
    print(f"  Tuned channel {ch}.")


def pin_igi(transport, igi):
    for addr in REG_DIG_PATH:
        transport.write32_mask(addr, DIG_IGI_MASK, igi & DIG_IGI_MASK)


def capture(dev, ep_in, dwell):
    """Return {ssid: [pwdb,...]} for page-0 beacons over `dwell` seconds."""
    out = defaultdict(list)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < dwell:
        buf = read_rx_burst(dev, ep_in, max_size=16384, timeout_ms=200)
        if buf is None:
            continue
        pos = 0
        while pos + RX_PKT_DESC_SZ <= len(buf):
            try:
                stat = parse_rx_pkt_desc(buf, pos)
            except ValueError:
                break
            if stat.pkt_len == 0 or stat.total_size == 0 or pos + stat.total_size > len(buf):
                break
            if not stat.is_c2h and stat.phy_status_present and stat.drv_info_sz >= 8:
                po = pos + RX_PKT_DESC_SZ
                if po + 4 <= len(buf) and (buf[po] & 0xF) == 0:  # page 0 (CCK)
                    mpdu = bytes(buf[pos + stat.mpdu_offset: pos + stat.mpdu_offset + max(stat.pkt_len - 4, 0)])
                    parsed = WlanFrameParser.parse_80211_frame(mpdu, -100)
                    if parsed and parsed.subtype_id == WlanFrameParser.SUBTYPE_BEACON:
                        out[ssid_of(mpdu) or "<hidden>"].append(buf[po + 1])  # P0 PWDB
            nxt = (pos + stat.total_size + 7) & ~7
            if nxt <= pos:
                break
            pos = nxt
    return out


def med(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel", type=int, default=1)
    args = p.parse_args()

    dev = open_device()
    transport = RTL8822BUTransport(dev)
    bring_up(dev, transport, args.channel)
    ep_in = probe_endpoints(dev).primary_bulk_in
    print(f"  AGC-default IGI = 0x{transport.read32(REG_DIG_PATH[0]) & DIG_IGI_MASK:02x}\n")

    # per-IGI: {ssid: median pwdb}
    table = {}
    ssids = set()
    try:
        for igi in IGI_LIST:
            pin_igi(transport, igi)
            time.sleep(0.4)
            capture(dev, ep_in, 0.5)  # discard settle window
            data = capture(dev, ep_in, DWELL_S)
            table[igi] = {s: med(v) for s, v in data.items()}
            ssids.update(data.keys())
            n = sum(len(v) for v in data.values())
            print(f"  IGI 0x{igi:02x}: {n} CCK beacons across {len(data)} SSIDs")
    finally:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)

    ssids = sorted(ssids)
    print("\n=== page-0 (CCK) median PWDB vs pinned IGI (dBm = PWDB-110) ===")
    hdr = "ssid".ljust(24) + "".join(f"  0x{igi:02x}" for igi in IGI_LIST)
    print(hdr)
    for s in ssids:
        row = s[:24].ljust(24)
        for igi in IGI_LIST:
            pw = table[igi].get(s)
            row += f"  {pw - 110:+4d}" if pw is not None else "    . "
        print(row)
    print("\n(If a row is flat ~+141 across all IGI -> CCK PWDB ignores OFDM IGI; "
          "the 2.4 saturation is in the CCK front-end, not DIG's reach.)")


if __name__ == "__main__":
    main()
