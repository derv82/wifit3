"""RTL8822BU RSSI forensics — diagnose the 0 dBm gaps + 2.4-weak/5-strong gap.

Cold-brings-up (or rides a warm chip), hops a few 2.4 + 5 GHz channels, and for
every beacon dumps the raw rx_pkt_desc fields that feed RSSI:

  - drv_info_sz, shift, physt, rate
  - phy_status page + PWDB read at desc_end (our current offset, NO shift)
  - phy_status page + PWDB read at desc_end+shift (kernel-correct offset)

Then a per-SSID summary of the dBm both ways, so we can see (a) whether shift is
ever nonzero, and (b) the true 2.4-vs-5 GHz PWDB spread.

Usage:
    .venv/Scripts/python.exe scripts/rtl8822bu/diag_rssi.py
"""
from __future__ import annotations

import struct
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
from wifit3.chips.rtl8822bu.firmware import download_firmware, download_firmware_validate, load_firmware_blob
from wifit3.chips.rtl8822bu.mac import (
    cut_mask_from_sys_cfg1, is_chip_warm, mac_init_for_rx, mac_power_on,
)
from wifit3.chips.rtl8822bu.phy import EfuseDefaults, phy_set_param
from wifit3.chips.rtl8822bu.rx import RX_PKT_DESC_SZ, parse_rx_pkt_desc, probe_endpoints, read_rx_burst
from wifit3.chips.rtl8822bu.transport import RTL8822BUTransport
from wifit3.wlan.packet import WlanFrameParser

CH_2G = [1, 6, 11]
CH_5G = [36, 149]
DWELL_S = 3.0


def decode_phy(buf: bytes, off: int):
    """Return (page, pwdb_a, pwdb_b) for the phy_status word at `off`, or None."""
    if off < 0 or off + 4 > len(buf):
        return None
    page = buf[off] & 0xF
    return (page, buf[off + 1], buf[off + 2])


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
    print("No RTL8822BU found (is the UI holding it open? close it first).")
    sys.exit(1)


def bring_up(dev, transport):
    if is_chip_warm(transport):
        print("  Chip WARM — skipping cold bring-up, riding existing init.")
        return
    print("  Chip COLD — full bring-up...")
    cut_mask = cut_mask_from_sys_cfg1(transport.read32(REG_SYS_CFG1))
    mac_power_on(transport, cut_mask=cut_mask)
    download_firmware(dev, transport, load_firmware_blob())
    ok, _ = download_firmware_validate(transport)
    if not ok:
        print("  FW validate FAILED"); sys.exit(1)
    phy_set_param(transport, EfuseDefaults())
    mac_init_for_rx(transport)
    print("  bring-up done")


def capture_channel(dev, transport, ep_in, ch: int, is_2g: bool, agg: dict):
    if is_2g:
        set_channel_2g_20mhz(transport, ch)
    else:
        set_channel_5g_20mhz(transport, ch)
    band = "2G" if is_2g else "5G"
    t0 = time.perf_counter()
    frames = 0
    while time.perf_counter() - t0 < DWELL_S:
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
            mpdu = bytes(buf[pos + stat.mpdu_offset: pos + stat.mpdu_offset + max(stat.pkt_len - 4, 0)])
            if not stat.is_c2h and stat.phy_status_present and stat.drv_info_sz >= 8:
                parsed = WlanFrameParser.parse_80211_frame(mpdu, -100)
                if parsed and parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON:
                    ssid = ssid_of(mpdu) or "<hidden>"
                    noshift = decode_phy(buf, pos + RX_PKT_DESC_SZ)
                    withshift = decode_phy(buf, pos + RX_PKT_DESC_SZ + stat.shift)
                    key = (band, ch, ssid)
                    rec = agg[key]
                    rec["n"] += 1
                    rec["shift"][stat.shift] += 1
                    rec["drv"][stat.drv_info_sz] += 1
                    rec["rate"][stat.rate] += 1
                    if noshift:
                        rec["ns_page"][noshift[0]] += 1
                        rec["ns_pwdb"].append(max(noshift[1], noshift[2]) if noshift[0] == 1 else noshift[1])
                    if withshift:
                        rec["ws_page"][withshift[0]] += 1
                        rec["ws_pwdb"].append(max(withshift[1], withshift[2]) if withshift[0] == 1 else withshift[1])
                    frames += 1
            nxt = (pos + stat.total_size + 7) & ~7
            if nxt <= pos:
                break
            pos = nxt
    print(f"  {band} ch{ch}: {frames} beacon phy-status samples")


def _newrec():
    return {
        "n": 0,
        "shift": defaultdict(int), "drv": defaultdict(int), "rate": defaultdict(int),
        "ns_page": defaultdict(int), "ws_page": defaultdict(int),
        "ns_pwdb": [], "ws_pwdb": [],
    }


def main():
    dev = open_device()
    transport = RTL8822BUTransport(dev)
    bring_up(dev, transport)
    ep_in = probe_endpoints(dev).primary_bulk_in
    agg = defaultdict(_newrec)
    try:
        for ch in CH_2G:
            capture_channel(dev, transport, ep_in, ch, True, agg)
        for ch in CH_5G:
            capture_channel(dev, transport, ep_in, ch, False, agg)
    finally:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)

    print("\n=== Per-SSID phy-status forensics ===")
    print(f"{'band':4} {'ch':>3} {'ssid':22} {'n':>4} {'shift':>10} "
          f"{'ns_pg':>6} {'ws_pg':>6} {'ns_dBm(med)':>11} {'ws_dBm(med)':>11}")

    def med(xs):
        if not xs:
            return None
        s = sorted(xs)
        return s[len(s) // 2]

    for (band, ch, ssid), r in sorted(agg.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        shiftstr = ",".join(f"{k}:{v}" for k, v in sorted(r["shift"].items()))
        ns_pg = ",".join(f"{k}:{v}" for k, v in sorted(r["ns_page"].items()))
        ws_pg = ",".join(f"{k}:{v}" for k, v in sorted(r["ws_page"].items()))
        ns_med = med(r["ns_pwdb"])
        ws_med = med(r["ws_pwdb"])
        ns_dbm = f"{ns_med - 110:+d}" if ns_med is not None else "-"
        ws_dbm = f"{ws_med - 110:+d}" if ws_med is not None else "-"
        print(f"{band:4} {ch:>3} {ssid[:22]:22} {r['n']:>4} {shiftstr:>10} "
              f"{ns_pg:>6} {ws_pg:>6} {ns_dbm:>11} {ws_dbm:>11}")

    # Global shift histogram — the key question.
    allshift = defaultdict(int)
    for r in agg.values():
        for k, v in r["shift"].items():
            allshift[k] += v
    print(f"\nGlobal shift histogram (beacons): {dict(sorted(allshift.items()))}")
    print("  -> if any shift != 0 appears, our phy_status offset (which omits "
          "shift) is reading the wrong bytes for those frames.")


if __name__ == "__main__":
    main()
