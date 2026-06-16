"""RTL8822BU (DKMS port) — live hardware smoke test: cold init + monitor RX (beacons).

Passive: control transfers + firmware page-writes + monitor bulk-IN RX only. No 802.11 TX.

Phases:
  open   : USB claim + chip-ID read (rejects implausible values).
  init   : open, then bringup.cold_bringup (the byte-for-byte-verified two-cycle cold init).
  beacon : init, then for each 2.4 GHz channel set_channel_bw + open monitor RCR + a synchronous
           bulk-IN loop, counting beacons per channel. Confirms 2.4 GHz monitor RX works.

Usage (card plugged in, WinUSB-bound via Zadig on Windows):
    uv run python scripts/rtl8822bu_dkms/test_hw.py --phase init
    uv run python scripts/rtl8822bu_dkms/test_hw.py --phase beacon --dwell 2.5
    uv run python scripts/rtl8822bu_dkms/test_hw.py --phase beacon --channel 6 --dwell 15
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

from wifit3.chips.rtl8822bu_dkms import bringup, chan, chipid, mac, rx
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport
from wifit3.wlan.packet import WlanFrameParser

USB_VID, USB_PID = 0x2357, 0x0138
CHANNELS_2G = list(range(1, 14))


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] RTL8822BU not found ({USB_VID:04x}:{USB_PID:04x}). "
              "Plug it in, confirm Zadig bound it to WinUSB.")
        return None
    print(f"[*] Found RTL8822BU at bus {dev.bus}, address {dev.address}")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.debug("set_configuration: %s", e)
    return dev


def _force_igi(t, igi: int) -> None:
    """Override RX gain (IGI [6:0]) on both paths — bypasses the dig_init seed for the DIG test."""
    for reg in (0x0C50, 0x0E50):
        v = t.read32(reg)
        t.write32(reg, (v & ~0x7F) | (igi & 0x7F))


def _dwell_count(t, dwell, rssi, total):
    """One dwell window: bulk-IN loop, tally beacons. Returns (beacons, bufs, bytes, frames, total)."""
    beacons: Counter = Counter()
    raw_bytes = raw_bufs = ch_frames = 0
    start = time.monotonic()
    while time.monotonic() - start < dwell:
        buf = t.bulk_in()
        if not buf:
            continue
        raw_bufs += 1
        raw_bytes += len(buf)
        for frame, r in rx.iter_frames(buf):
            total += 1
            ch_frames += 1
            parsed = WlanFrameParser.parse_80211_frame(frame, r)
            if not parsed or parsed.get("type") != "beacon":
                continue
            b = (parsed.get("bssid") or "").lower()
            if not b or b == "ff:ff:ff:ff:ff:ff":
                continue
            beacons[b] += 1
            if r and (b not in rssi or r > rssi[b]):
                rssi[b] = r
    return beacons, raw_bufs, raw_bytes, ch_frames, total


def _watch(t, channels, dwell: float, prev_ch, igi=None, rcr=None):
    """Tune each channel, then a bulk-IN loop for `dwell` s; tally beacons. `igi` forces RX gain to a
    hex value or sweeps a range (DIG-watchdog hypothesis test); `rcr` overrides the monitor RCR. rx-dma
    bytes vs parsed frames split "no bytes off USB" (RX-DMA gap) from "bytes but no good frames"."""
    per_ch: dict[int, Counter] = {}
    rssi: dict[str, int] = {}
    total = 0
    igis = ([None] if not igi
            else [0x1C, 0x24, 0x2C, 0x34, 0x3C, 0x44] if igi == "sweep" else [int(igi, 0)])
    mac.enable_monitor(t)                          # faithful airmon monitor RX-enable (once)
    if rcr is not None:
        t.write32(0x0608, int(rcr, 0))             # diagnostic RCR override (e.g. accept CRC/ICV errors)
    for ch in channels:
        chan.set_channel_bw(t, ch, prev_ch=prev_ch)
        prev_ch = ch
        for g in igis:
            if g is not None:
                _force_igi(t, g)
            beacons, bufs, nbytes, frames, total = _dwell_count(t, dwell / len(igis), rssi, total)
            per_ch[ch] = per_ch.get(ch, Counter()) + beacons
            tag = f" IGI=0x{g:02x}" if g is not None else ""
            print(f"    ch {ch:>3}{tag}: {len(beacons):>2} APs, {sum(beacons.values()):>4} beacons  "
                  f"[rx-dma {bufs} bufs / {nbytes} B, {frames} frames]")
    return per_ch, rssi, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("open", "init", "beacon"), default="beacon")
    ap.add_argument("--channel", type=int, default=None, help="single channel (default: hop 1-13)")
    ap.add_argument("--dwell", type=float, default=2.5, help="seconds per channel")
    ap.add_argument("--igi", default=None,
                    help="force RX gain IGI to a hex value (0x30) or 'sweep' (try a range). "
                         "Tests the DIG-watchdog hypothesis: without the runtime DIG, IGI is frozen "
                         "at the dig_init seed, which may be too low (saturating FA -> no demod).")
    ap.add_argument("--rcr", default=None,
                    help="override the monitor RCR after enable_monitor (hex, e.g. 0x90000301 to "
                         "ACCEPT CRC/ICV-error frames). Diagnostic: if bytes arrive only with errors "
                         "accepted, the BB demods but the CRC fails (RF/BB offset), not an RX-DMA gap.")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}  (a running wifit3 may hold the card)")

    t = Rtl8822buTransport(dev)
    try:
        info = chipid.get_chip_info(t)
        print(f"  chip_ver (cut) = {info.chip_ver}")
        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        print("[*] running cold bring-up (two-cycle init: chip-ID/EFUSE/FW/MAC/BB/RF)...")
        bringup.cold_bringup(t)
        print("[PASS] cold init complete (no bus errors).")
        if args.phase == "init":
            return 0

        channels = [args.channel] if args.channel else CHANNELS_2G
        dwell = args.dwell if args.channel else args.dwell
        igi_note = f", IGI={args.igi}" if args.igi else ""
        print(f"[*] monitor RX: {'channel ' + str(args.channel) if args.channel else 'hop 1-13'}, "
              f"{dwell:g}s/ch{igi_note}...")
        per_ch, rssi, frames = _watch(t, channels, dwell, prev_ch=None, igi=args.igi, rcr=args.rcr)

        allb: Counter = Counter()
        for c in per_ch.values():
            allb.update(c)
        total = sum(allb.values())
        print(f"\n[RESULT] {len(allb)} unique APs, {total} beacons total, {frames} frames seen")
        for b, n in allb.most_common(20):
            print(f"    {b}  {n:>4}  {rssi.get(b, '?')} dBm")
        if not allb:
            return _fail("no beacons heard — RX path not delivering frames "
                         "(check RCR / RX-DMA / AGC gain).")
        print("[PASS] 2.4 GHz monitor RX hears beacons.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
