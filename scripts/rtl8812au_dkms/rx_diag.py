"""RX demod diagnostic for the 8812au DKMS port.

Distinguishes the two RX-deaf failure modes after the board-param (external-gain) fix:
  * demod BROKEN  -> bulk-IN delivers garbage MPDUs (random frame-control, bad CRC)
  * demod FINE    -> real 802.11 frames arrive; only the RX filter was dropping them

Brings up with EFUSE-derived phy_cond params (same as trace_bringup), enters monitor,
then overrides RCR with a PERMISSIVE value that ACCEPTS CRC/ICV-error frames
(0x9000382F | ACRC32(bit8) | AICV(bit9)) so even garbage is delivered. Classifies every
frame as valid-beacon / valid-other / unparseable and dumps the first few raw MPDUs.

Passive — RX only, no 802.11 TX.

    uv run python scripts/rtl8812au_dkms/rx_diag.py [--secs 10] [--rcr 0x90003B2F] [--channel 1]
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

from wifit3.chips.rtl88xxau_base import registers as R
from wifit3.chips.rtl88xxau_base.transport import Rtl88xxauTransport
from wifit3.chips.rtl8812au_dkms import bb, chan, dig, efuse, firmware, iqk, mac, monitor, rf, rx, txpower
from wifit3.chips.rtl8812au_dkms.constants import USB_PID_AWUS036ACH, USB_VID_REALTEK
from wifit3.wlan.packet import WlanFrameParser

REG_RCR = 0x0608
RCR_PERMISSIVE = 0x9000382F | (1 << 8) | (1 << 9)   # monitor RCR + ACRC32 + AICV


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACH, backend=backend)
    if dev is None:
        print(f"[FAIL] AWUS036ACH not found ({USB_VID_REALTEK:04x}:{USB_PID_AWUS036ACH:04x}).")
        return None
    print(f"[*] Found AWUS036ACH at bus {dev.bus}, address {dev.address}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--rcr", type=lambda s: int(s, 0), default=RCR_PERMISSIVE)
    ap.add_argument("--board-type", type=lambda s: int(s, 0), default=None,
                    help="override EFUSE board_type (e.g. 0 = internal-gain branch)")
    ap.add_argument("--dump", type=int, default=8, help="raw MPDUs to dump")
    ap.add_argument("--no-iqk", action="store_true", help="skip IQK (A/B test)")
    ap.add_argument("--no-edcca", action="store_true", help="skip the live PWDB-EDCCA search (A/B test)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        print(f"[FAIL] claim_interface(0): {e}")
        return 1

    t = Rtl88xxauTransport(dev)
    try:
        sys_cfg = t.read32(R.REG_SYS_CFG)
        if sys_cfg in (0, 0xFFFFFFFF):
            print("[FAIL] implausible REG_SYS_CFG — unplug 5s, replug, rerun.")
            return 1
        params = efuse.read_chip_params(t)
        jp = efuse.build_jaguar_params(params, sys_cfg)
        if args.board_type is not None:
            jp.board_type = args.board_type
        print(f"  board_type=0x{jp.board_type:02x} cut_version={jp.cut_version} rfe_type={params.rfe_type}")
        fw = firmware.load_firmware_blob()
        if not firmware.bring_up(t, fw):
            return 1
        mac.phy_mac_config(t)
        mac.mac_init_misc(t)
        bb.phy_bb_config(t, crystal_cap=params.crystal_cap, params=jp)
        rf.phy_rf_config(t, params=jp)
        chan.set_chnl_bw(t, ch=args.channel, bb_swing_2g_a=params.bb_swing_2g[0],
                         bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type)
        txpower.set_tx_power(t, args.channel, params.tx_power_2g)
        if not args.no_iqk:
            print("[*] running IQK (PHY_IQCalibrate_8812A)...")
            try:
                iqk.iq_calibrate(t, is_2g=True)
            except Exception as e:  # noqa: BLE001 - diagnostic; surface and continue
                print(f"  [IQK ERROR] {type(e).__name__}: {e}")
            rxa, rxb = t.read32(0x0C10), t.read32(0x0E10)
            print(f"  IQK RX-IQC: 0xC10=0x{rxa:08x} 0xE10=0x{rxb:08x} "
                  f"(0x100 low-byte default => cal did not take)")
        mac.hal_init_misc_pre(t)
        dig.init_hal_dm(t, search_edcca=not args.no_edcca)   # live PWDB-EDCCA search (morrownr runs it)
        mac.hal_init_misc_post(t)
        monitor.set_monitor_mode(t, args.channel, params)   # morrownr's monitor opmode + set-channel tail

        t.write32(REG_RCR, args.rcr)
        rcr = t.read32(REG_RCR)
        print(f"[*] RCR set permissive: wrote 0x{args.rcr:08x} readback 0x{rcr:08x} "
              f"(ACRC32={bool(rcr & (1 << 8))} AICV={bool(rcr & (1 << 9))})")
        print(f"[*] RX ch{args.channel} for {args.secs:g}s ...")

        total = 0
        valid = 0
        beacons: Counter = Counter()
        fc0 = Counter()
        dumped = 0
        raw_calls = raw_nonempty = raw_bytes = raw_dumped = 0
        start = time.monotonic()
        while time.monotonic() - start < args.secs:
            buf = t.bulk_in()
            raw_calls += 1
            if not buf:
                continue
            raw_nonempty += 1
            raw_bytes += len(buf)
            if raw_dumped < 4:
                raw_dumped += 1
                print(f"  rawbuf[{raw_nonempty}] len={len(buf)} {bytes(buf[:64]).hex()}")
            for frame, r in rx.iter_frames(buf):
                total += 1
                if frame:
                    fc0[frame[0]] += 1
                if dumped < args.dump:
                    dumped += 1
                    print(f"  raw[{total}] rssi={r} len={len(frame)} "
                          f"{bytes(frame[:36]).hex()}")
                parsed = WlanFrameParser.parse_80211_frame(frame, r)
                if not parsed:
                    continue
                valid += 1
                if parsed.get("type") == "beacon":
                    b = (parsed.get("bssid") or "").lower()
                    if b and b != "ff:ff:ff:ff:ff:ff":
                        beacons[b] += 1
        elapsed = max(time.monotonic() - start, 1e-3)
        print(f"\n[BULK-IN] {raw_calls} reads, {raw_nonempty} non-empty, {raw_bytes} bytes total")
        print(f"[RESULT] {total} frames ({total / elapsed:.0f}/s), {valid} parsed-valid, "
              f"{len(beacons)} beacon-BSSIDs ({sum(beacons.values())} beacons)")
        print(f"  frame-control byte0 histogram (top 12): "
              f"{', '.join(f'0x{k:02x}:{v}' for k, v in fc0.most_common(12))}")
        for b, n in beacons.most_common(10):
            print(f"    beacon {b}  x{n}")
        if total == 0:
            print("  => 0 frames even with CRC-accept: HW delivering NOTHING (RX path/DMA, not demod-garbage).")
        elif not beacons:
            print("  => frames flow but none are valid beacons: demod still producing garbage.")
        else:
            print("  => REAL beacons present: demod works; the strict monitor RCR was dropping them.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
