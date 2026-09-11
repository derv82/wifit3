"""RTL8821AU (DKMS port) — live hardware smoke test of the implemented bring-up.

Passive: control transfers, firmware page-writes, and monitor RX only. No 802.11
TX/inject.

Phases (cumulative):
  open   : USB claim + REG_SYS_CFG sanity read (cold-boot ground truth 0x04412135).
  fw     : open, then firmware.bring_up (power-on -> LLT -> FW download -> FW-ready),
           checking REG_MCUFWDL ends with WINTINI_RDY set.
  mac    : fw, then M2 MAC init (REG_CR -> MACTXEN|MACRXEN).
  phy    : mac, then M3 BB/RF init (xtal 0x9e7).
  chan   : phy, then M4 2.4 GHz band + ch1 + 20 MHz BW.
  beacon : full driver bring-up (M1-M5) + a fixed-channel monitor-RX beacon count
           (the A/B headline) — nAPs, total + peak beacons/s, and the NETGEAR2G canary.

Usage (card plugged in, WinUSB-bound via Zadig on Windows):
    uv run python scripts/chips/rtl8821au_dkms/test_hw.py                       # = chan
    uv run python scripts/chips/rtl8821au_dkms/test_hw.py --phase beacon        # ch1, 30s
    uv run python scripts/chips/rtl8821au_dkms/test_hw.py --phase beacon --channel 6 --duration 60
    uv run python scripts/chips/rtl8821au_dkms/test_hw.py --phase open --debug
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from _hwstop import interruptible_sleep

from wifit3.chips.rtl8821au_dkms import SUPPORTED_IDS, constants as C
from wifit3.chips.rtl8821au_dkms import bb, chan, firmware, mac, rf
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
from wifit3.chips.rtl8821au_dkms.transport import RTL8821AUDkmsTransport

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet

# A/B canary — strong nearby AP whose beacon rate is the DIG-health indicator. This
# BSSID is the deliberately-committed fixed canary (on the git-history PII-scrub list);
# see chips/rtl8821au_dkms/RTL8821AU_DKMS.md. Baseline (mainline, ch1/30s): ~7.7/s.
DEFAULT_CANARY = "aa:bb:cc:dd:ee:01"


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _open_device():
    """Find, detach, configure, and claim any supported RTL8821AU/RTL8811AU adapter."""
    backend = libusb_package.get_libusb1_backend()
    for id_entry in SUPPORTED_IDS:
        dev = usb.core.find(idVendor=id_entry.vid, idProduct=id_entry.pid, backend=backend)
        if dev is None:
            continue
        print(f"[*] Found {id_entry.description} ({id_entry.vid:04x}:{id_entry.pid:04x}) "
              f"at bus {dev.bus}, address {dev.address}")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            logging.debug("set_configuration: %s", e)
        return dev, id_entry
    supported = ", ".join(f"{entry.vid:04x}:{entry.pid:04x}" for entry in SUPPORTED_IDS)
    print("[FAIL] No supported RTL8821AU/RTL8811AU adapter found. "
          "Plug it in, confirm Zadig bound it to WinUSB. "
          f"Supported VID:PIDs: {supported}")
    return None, None


class BeaconTally:
    """Driver rx callback: counts beacons + tracks strongest RSSI per BSSID."""

    def __init__(self) -> None:
        self.by_bssid: Counter = Counter()
        self.rssi: dict = {}        # bssid -> strongest (max) dBm seen
        self.channel: dict = {}     # bssid -> advertised channel (DS/HT/VHT IE)
        self.total_frames = 0

    def __call__(self, parsed: Packet) -> None:
        self.total_frames += 1
        if parsed.type != "beacon":
            return
        bssid = (parsed.bssid or "").lower()
        if not bssid or bssid == "ff:ff:ff:ff:ff:ff":
            return
        self.by_bssid[bssid] += 1
        # Track the strongest (max) real dBm; skip the 0 "unknown" sentinel (frames with
        # no PHY status) so it can't mask a real negative value.
        r = parsed.rssi
        if r and (bssid not in self.rssi or r > self.rssi[bssid]):
            self.rssi[bssid] = r
        ch = parsed.channel   # the AP's own advertised channel
        if ch:
            self.channel[bssid] = ch


async def _run_beacon(args) -> int:
    dev, id_entry = _open_device()
    if dev is None or id_entry is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}  (a running wifit3 may hold the card)")

    driver = Rtl8821auDkmsDriver.from_usb_device(dev, id_entry)
    driver.enable_dig = not args.no_dig     # A/B: isolate the DIG watchdog's effect
    tally = BeaconTally()
    driver.register_rx_callback(tally)

    def progress(pct, msg):
        print(f"  [{pct * 100:5.1f}%] {msg}")

    if not await driver.connect(progress):
        await driver.close()
        return _fail("bring-up did not reach FW-ready")

    # Channel plan: hop a band (--band) to discover where APs live, else a fixed channel.
    supported = Rtl8821auDkmsDriver.SUPPORTED_CHANNELS
    if args.band == "5g":
        channels, what = [c for c in supported if c > 14], "5 GHz hop"
    elif args.band == "2g":
        channels, what = [c for c in supported if c <= 14], "2.4 GHz hop"
    elif args.band == "all":
        channels, what = list(supported), "all-band hop"
    else:
        channels, what = [args.channel], f"ch{args.channel} fixed"
    print(f"\n[*] {what} for {args.duration:g}s (dwell {args.dwell:g}s) ...  "
          f"DIG watchdog: {'OFF' if args.no_dig else 'ON'}")
    start = time.monotonic()
    i = 0
    try:
        while time.monotonic() - start < args.duration:
            cur = channels[i % len(channels)]
            await driver.set_channel(cur)
            i += 1
            await interruptible_sleep(args.dwell)
            print(f"\r  {time.monotonic() - start:4.0f}s ch{cur:>3}  nAPs={len(tally.by_bssid)}  "
                  f"beacons={sum(tally.by_bssid.values())}  frames={tally.total_frames}", end="")
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[stopping — Ctrl+C]")
    print()
    elapsed = max(time.monotonic() - start, 1e-3)
    await driver.close()

    n_aps = len(tally.by_bssid)
    total = sum(tally.by_bssid.values())
    print(f"\n[RESULT] {what}, {elapsed:.0f}s: {n_aps} unique AP(s), {total} beacons "
          f"({total / elapsed:.1f}/s total), {tally.total_frames} frames")
    if tally.by_bssid:
        print("  top BSSIDs (bssid / advertised ch / beacons / strongest RSSI):")
        for bssid, n in tally.by_bssid.most_common(20):
            print(f"    {bssid}  ch{str(tally.channel.get(bssid, '?')):>3}  {n:>4}  "
                  f"{tally.rssi.get(bssid, '?')} dBm")
    else:
        print("  (no beacons — check antenna/channel; this is the live RX gate)")

    canary = args.canary.lower()
    c_n = tally.by_bssid.get(canary, 0)
    print(f"\n  canary {canary}: {c_n} beacons ({c_n / elapsed:.1f}/s), "
          f"{tally.rssi.get(canary, '?')} dBm  "
          f"[mainline baseline ~7.7/s; healthy ~9-10/s — DIG-health indicator]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("open", "fw", "mac", "phy", "chan", "beacon"),
                    default="chan")
    ap.add_argument("--channel", type=int, default=1, help="fixed beacon-phase channel")
    ap.add_argument("--band", choices=("2g", "5g", "all"), default=None,
                    help="hop this band's channels instead of a fixed --channel")
    ap.add_argument("--dwell", type=float, default=2.0, help="per-channel dwell when hopping (s)")
    ap.add_argument("--duration", type=float, default=30.0, help="beacon-phase total time (s)")
    ap.add_argument("--no-dig", action="store_true", help="disable the DIG watchdog (A/B)")
    ap.add_argument("--canary", default=DEFAULT_CANARY, help="A/B canary BSSID")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.phase == "beacon":
        try:
            return asyncio.run(_run_beacon(args))
        except KeyboardInterrupt:
            return 130

    dev, _id_entry = _open_device()
    if dev is None or _id_entry is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}")

    t = RTL8821AUDkmsTransport(dev)
    rc = 0
    try:
        sys_cfg = t.read32(C.REG_SYS_CFG)
        print(f"  REG_SYS_CFG (0xF0) = 0x{sys_cfg:08x}  (cold-boot ground truth 0x04412135)")
        if sys_cfg in (0, 0xFFFFFFFF):
            return _fail("implausible REG_SYS_CFG — unplug 5s, replug, rerun.")

        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        fw = firmware.load_firmware_blob()
        print(f"[*] FW blob {len(fw)} bytes; running bring_up()...")
        ready = firmware.bring_up(t, fw)
        mcu = t.read32(C.REG_MCUFWDL)
        bits = [n for n, b in (("MCUFWDL_RDY", C.MCUFWDL_RDY), ("FWDL_ChkSum_rpt", C.FWDL_ChkSum_rpt),
                               ("WINTINI_RDY", C.WINTINI_RDY), ("RAM_DL_SEL", C.RAM_DL_SEL))
                if mcu & b]
        print(f"  REG_MCUFWDL (0x80) = 0x{mcu:08x}  set: {bits or '(none)'}")
        if not ready:
            return _fail("bring_up did not reach FW-ready (WINTINI_RDY).")
        print("[PASS] FW-ready (WINTINI_RDY) — wlan CPU is running the firmware.")

        if args.phase in ("mac", "phy", "chan"):
            print("[*] running MAC init (M2)...")
            mac.phy_mac_config(t)
            mac.mac_init_misc(t)
            cr = t.read8(C.REG_CR)
            print(f"  REG_CR (0x100) = 0x{cr:02x}  "
                  f"MACTXEN={bool(cr & mac.MACTXEN)} MACRXEN={bool(cr & mac.MACRXEN)}")
            if not (cr & mac.MACTXEN and cr & mac.MACRXEN):
                return _fail("REG_CR missing MACTXEN|MACRXEN after MAC init.")
            print("[PASS] MAC enabled (REG_CR MACTXEN|MACRXEN).")

        if args.phase in ("phy", "chan"):
            print("[*] running PHY init (M3: BB PHY_REG/AGC + crystal_cap + RadioA)...")
            bb.phy_bb_config(t, crystal_cap=0x27)   # TODO(efuse): read crystal_cap from EFUSE
            rf.phy_rf_config(t)
            xtal = t.read32(0x002C)
            print(f"  REG 0x2C = 0x{xtal:08x}  (xtal field [23:12] = 0x{(xtal >> 12) & 0xFFF:03x}, "
                  f"expect 0x9e7 for crystal_cap 0x27)")
            print("[PASS] PHY (BB + RF) init complete — no bus errors.")

        if args.phase == "chan":
            print("[*] running channel tune (M4: 2.4 GHz band + ch1 + 20 MHz BW)...")
            chan.set_chnl_bw(t, ch=1)
            rf18 = rf._rf_serial_read(t, rf.RF_PATH_A, rf.RF_CHNLBW)
            print(f"  RF[0x18] = 0x{rf18:05x}  (channel/BW reg — ch1 @ 20 MHz)")
            print("[PASS] channel tune complete — no bus errors.")
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
