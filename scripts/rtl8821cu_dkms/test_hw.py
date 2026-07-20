"""RTL8821CU (8821cu_dkms) — live hardware smoke test: cold init + monitor RX (beacons).

Passive: control transfers + firmware page-writes (bulk-OUT ep 0x05) + monitor bulk-IN RX only.
No 802.11 TX (no injection / deauth).

Phases:
  open : USB claim + chip-ID read (rejects implausible values) — confirms WinUSB binding + the
         bRequest-0x05 control plumbing.
  init : open, then bringup.cold_bringup — the byte-for-byte-verified cold init (chip-ID/EFUSE,
         iDDMA firmware download, MAC/BB/RF, BT-coex, and the channel tune to ch 1 / 20 MHz). This
         is where real silicon catches what the offline replay can't: the FW actually booting
         (0xC078 ready poll) and live register read-backs feeding the RMW math.
  rx   : init, then a synchronous bulk-IN loop on the tuned channel (1), counting raw bytes and
         decoding the HALMAC rx_pkt_desc to tally beacons. Confirms 2.4 GHz monitor RX delivers.

Usage (card in Wi-Fi mode 0bda:c820, WinUSB-bound via Zadig on Windows):
    uv run python scripts/rtl8821cu_dkms/test_hw.py --phase open
    uv run python scripts/rtl8821cu_dkms/test_hw.py --phase init
    uv run python scripts/rtl8821cu_dkms/test_hw.py --phase rx --dwell 8
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chipid
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport
from wifit3.dot11.parser import WlanFrameParser

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05               # FW/TX bulk-OUT is on ep 0x05 (NOT the 0x04 default)


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


_WIFI_INTF_CLASS = 0xFF             # the WiFi interface (vendor-specific); 0xE0 = the BT interfaces


def _wifi_interface(dev) -> int:
    """The combo card's WiFi interface number — the vendor-specific (class 0xFF) one (interface 2),
    NOT the Bluetooth interfaces 0/1 (class 0xE0). Zadig must bind WinUSB to this interface."""
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == _WIFI_INTF_CLASS:
            return intf.bInterfaceNumber
    raise RuntimeError("no vendor-specific (WiFi) interface found")


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] RTL8821CU Wi-Fi function not found ({USB_VID:04x}:{USB_PID:04x}). "
              "If Windows shows a CD-ROM, the card is in ZeroCD mode — mode-switch it first. "
              "Then confirm Zadig bound it to WinUSB.")
        return None
    print(f"[*] Found RTL8821CU at bus {dev.bus}, address {dev.address}")
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)
    return dev


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def _rx_beacons(t, dwell: float):
    """Bulk-IN loop: tally raw bytes/buffers and walk each 24-byte HALMAC rx_pkt_desc, parsing the
    MPDU with WlanFrameParser and counting beacons by BSSID. Splits 'no bytes off USB' (RX-DMA gap)
    from 'bytes but no good frames' (decode/CRC/AGC)."""
    acc = {k: 0 for k in ("pkts", "good", "crc_err", "icv_err", "c2h", "beacons")}
    beacons: Counter = Counter()
    bufs = nbytes = 0
    start = time.monotonic()
    while time.monotonic() - start < dwell:
        buf = t.bulk_in()
        if not buf:
            continue
        bufs += 1
        nbytes += len(buf)
        off, n = 0, len(buf)
        while off + 24 <= n:
            w0 = int.from_bytes(buf[off:off + 4], "little")
            pkt_len = w0 & 0x3FFF
            crc_err, icv_err = (w0 >> 14) & 1, (w0 >> 15) & 1
            drvinfo_sz = ((w0 >> 16) & 0xF) << 3
            shift_sz = (w0 >> 24) & 0x3
            c2h = (int.from_bytes(buf[off + 8:off + 12], "little") >> 28) & 1
            if pkt_len <= 0:
                break
            mpdu_off = off + 24 + drvinfo_sz + shift_sz
            if mpdu_off + pkt_len > n:
                break
            acc["pkts"] += 1
            if c2h:
                acc["c2h"] += 1
            elif crc_err:
                acc["crc_err"] += 1
            elif icv_err:
                acc["icv_err"] += 1
            else:
                acc["good"] += 1
                parsed = WlanFrameParser.parse_80211_frame(buf[mpdu_off:mpdu_off + pkt_len], None)
                if parsed and parsed.type == "beacon":
                    b = (parsed.bssid or "").lower()
                    if b and b != "ff:ff:ff:ff:ff:ff":
                        beacons[b] += 1
                        acc["beacons"] += 1
            off += _rnd8(24 + drvinfo_sz + shift_sz + pkt_len)
    print(f"\n[RX] {dwell:g}s on ch 1: {bufs} bufs / {nbytes} B, {acc['pkts']} packets")
    print(f"  good={acc['good']}  crc_err={acc['crc_err']}  icv_err={acc['icv_err']}  c2h={acc['c2h']}")
    print(f"  beacons={acc['beacons']} from {len(beacons)} unique APs")
    for b, c in beacons.most_common(15):
        print(f"    {b}  {c}")
    return bufs, nbytes, beacons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("open", "init", "rx"), default="init")
    ap.add_argument("--dwell", type=float, default=8.0, help="seconds of bulk-IN RX (rx phase)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    dev = _open_device()
    if dev is None:
        return 1
    wifi_intf = _wifi_interface(dev)
    print(f"[*] WiFi (vendor) interface = {wifi_intf}  (BT is on interfaces 0/1)")
    try:
        usb.util.claim_interface(dev, wifi_intf)
    except (usb.core.USBError, NotImplementedError) as e:
        return _fail(f"claim_interface({wifi_intf}): {e}\n"
                     f"       Zadig must bind WinUSB to the WiFi interface (#{wifi_intf}, '802.11ac "
                     "NIC' 0bda:c820),\n       not the Bluetooth interfaces. Options -> List All "
                     "Devices -> the 0bda:c820 entry\n       whose interface is the vendor/WiFi one "
                     "-> Replace with WinUSB, then re-run.")

    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        chip_id, chip_ver = chipid.mount_get_chip_info(t)
        print(f"[*] chip_id=0x{chip_id:x}  chip_ver(cut)={chip_ver}")
        if not (0 <= chip_ver <= 15):
            return _fail(f"implausible chip_ver {chip_ver} — control transfers likely not working")
        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        print("[*] running cold bring-up (FW download + MAC/BB/RF + BT-coex + channel tune)...")
        bringup.cold_bringup(t)
        print("[PASS] cold init complete (FW booted, no bus errors, radio tuned to ch 1).")
        if args.phase == "init":
            return 0

        print(f"[*] monitor RX on ch 1 for {args.dwell:g}s...")
        bufs, nbytes, beacons = _rx_beacons(t, args.dwell)
        if nbytes == 0:
            return _fail("no bytes off the bulk-IN endpoint — RX-DMA not delivering (RX-enable / EP).")
        if not beacons:
            return _fail("bytes arrived but no beacons parsed — check the rx_pkt_desc decode / AGC.")
        print(f"[PASS] 2.4 GHz monitor RX hears {len(beacons)} APs.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, wifi_intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
