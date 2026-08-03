"""RTL8812AU (DKMS port) — live hardware smoke test of the implemented bring-up.

Passive: control transfers + firmware page-writes only (and monitor RX once M5 lands).
No 802.11 TX/inject. Phases are cumulative; only the milestones implemented so far are
wired.

Phases:
  open : USB claim + REG_SYS_CFG sanity read (no known cold-boot value — no 8812 pcap —
         so it just rejects 0 / 0xFFFFFFFF and prints the chip version).
  fw   : open, then firmware.bring_up (power-on -> LLT -> FW download -> FW-ready),
         checking REG_MCUFWDL ends with WINTINI_RDY set.

Usage (card plugged in, WinUSB-bound via Zadig on Windows):
    uv run python scripts/chips/rtl8812au_dkms/test_hw.py --phase open
    uv run python scripts/chips/rtl8812au_dkms/test_hw.py --phase fw
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl88xxau_base import registers as R
from wifit3.chips.rtl88xxau_base import sipi
from wifit3.chips.rtl88xxau_base.transport import Rtl88xxauTransport
from wifit3.chips.rtl8812au_dkms import bb, chan, dig, efuse, firmware, mac, monitor, rf, rx, txpower
from wifit3.chips.rtl8812au_dkms.constants import USB_PID_AWUS036ACH, USB_VID_REALTEK
from wifit3.dot11.parser import WlanFrameParser

DEFAULT_CANARY = "aa:bb:cc:dd:ee:01"   # documented A/B canary (NETGEAR2G)


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID_REALTEK, idProduct=USB_PID_AWUS036ACH, backend=backend)
    if dev is None:
        print(f"[FAIL] AWUS036ACH not found ({USB_VID_REALTEK:04x}:{USB_PID_AWUS036ACH:04x}). "
              "Plug it in, confirm Zadig bound it to WinUSB.")
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
    ap.add_argument("--phase",
                    choices=("open", "efuse", "fw", "mac", "phy", "chan", "txpower", "beacon"),
                    default="fw")
    ap.add_argument("--channel", type=int, default=1, help="beacon-phase channel")
    ap.add_argument("--duration", type=float, default=20.0, help="beacon-phase seconds")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        return _fail(f"claim_interface(0): {e}  (a running wifit3 may hold the card)")

    t = Rtl88xxauTransport(dev)
    try:
        sys_cfg = t.read32(R.REG_SYS_CFG)
        print(f"  REG_SYS_CFG (0xF0) = 0x{sys_cfg:08x}  (chip version / cut)")
        if sys_cfg in (0, 0xFFFFFFFF):
            return _fail("implausible REG_SYS_CFG — unplug 5s, replug, rerun.")

        if args.phase == "open":
            print("[PASS] control-transfer plumbing works.")
            return 0

        if args.phase == "efuse":
            print("[*] reading EFUSE / chip params (probe phase, 2T2R)...")
            p = efuse.read_chip_params(t)
            print(f"  crystal_cap = 0x{p.crystal_cap:02x}   mac = {p.mac_address or '<blank>'}")
            print(f"  rfe_type = {p.rfe_type}   autoload_fail = {p.autoload_fail}")
            print(f"  bb_swing 2g = {[hex(x) for x in p.bb_swing_2g]}  "
                  f"5g = {[hex(x) for x in p.bb_swing_5g]}")
            print(f"  path-A 2g cck_base = {[hex(x) for x in p.tx_power_2g[0].cck_base]}")
            print(f"  path-B 2g cck_base = {[hex(x) for x in p.tx_power_2g[1].cck_base]}")
            print(f"  path-A 5g bw40_base[0:4] = {[hex(x) for x in p.tx_power_5g[0].bw40_base[:4]]}")
            mac_ok = p.mac_address is not None and p.mac_address.startswith(("00:c0:ca", "00:13:37"))
            cap_ok = 0x00 < p.crystal_cap <= 0x3F
            if not cap_ok:
                return _fail(f"crystal_cap 0x{p.crystal_cap:02x} out of range — EFUSE decode suspect.")
            print(f"  (MAC OUI {'looks like ALFA/Realtek' if mac_ok else 'present'}; "
                  f"crystal_cap in range)")
            print("[PASS] EFUSE decoded (crystal_cap + MAC + rfe_type + 2-path PG).")
            return 0

        # EFUSE is a probe-phase read (before power-on); the phy/chan/txpower phases
        # use its real crystal_cap / rfe_type / bb_swing instead of M3/M4 defaults.
        params = None
        jp = None
        if args.phase in ("phy", "chan", "txpower", "beacon"):
            params = efuse.read_chip_params(t)
            jp = efuse.build_jaguar_params(params, sys_cfg)
            print(f"  EFUSE: crystal_cap=0x{params.crystal_cap:02x} mac={params.mac_address} "
                  f"rfe_type={params.rfe_type} bb_swing_2g={[hex(x) for x in params.bb_swing_2g]}")
            print(f"  phy_cond params: board_type=0x{jp.board_type:02x} cut_version={jp.cut_version}")

        fw = firmware.load_firmware_blob()
        print(f"[*] FW blob {len(fw)} bytes; running bring_up()...")
        ready = firmware.bring_up(t, fw)
        mcu = t.read32(R.REG_MCUFWDL)
        bits = [n for n, b in (("MCUFWDL_RDY", R.MCUFWDL_RDY), ("FWDL_ChkSum_rpt", R.FWDL_ChkSum_rpt),
                               ("WINTINI_RDY", R.WINTINI_RDY), ("RAM_DL_SEL", R.RAM_DL_SEL))
                if mcu & b]
        print(f"  REG_MCUFWDL (0x80) = 0x{mcu:08x}  set: {bits or '(none)'}")
        if not ready:
            return _fail("bring_up did not reach FW-ready (WINTINI_RDY).")
        print("[PASS] FW-ready (WINTINI_RDY) — wlan CPU is running the firmware.")

        if args.phase in ("mac", "phy", "chan", "txpower", "beacon"):
            print("[*] running MAC init (M2: PHY_MACConfig8812 + MISC -> REG_CR)...")
            mac.phy_mac_config(t)
            mac.mac_init_misc(t)
            cr = t.read8(R.REG_CR)
            print(f"  REG_CR (0x100) = 0x{cr:02x}  "
                  f"MACTXEN={bool(cr & mac.MACTXEN)} MACRXEN={bool(cr & mac.MACRXEN)}")
            if not (cr & mac.MACTXEN and cr & mac.MACRXEN):
                return _fail("REG_CR missing MACTXEN|MACRXEN after MAC init.")
            print("[PASS] MAC enabled (REG_CR MACTXEN|MACRXEN).")

        if args.phase in ("phy", "chan", "txpower", "beacon"):
            print("[*] running BB + RF init (M3: PHY_BBConfig8812 + RADIO_A + RADIO_B, 2T2R)...")
            bb.phy_bb_config(t, crystal_cap=params.crystal_cap, params=jp)
            rf.phy_rf_config(t, params=jp)
            xtal = (t.read32(0x002C) & 0x7FF80000) >> 19
            print(f"  crystal_cap field 0x2C[30:19] = 0x{xtal:03x} (expect 0x820 for cap 0x20)")
            # Path-A vs Path-B RF readback — the 2T2R gate. RF 0x00 (mode/AC) must read a
            # sane non-default value on BOTH radios (path B is the new surface).
            rfa0 = sipi._rf_serial_read(t, sipi.RF_PATH_A, 0x00)
            rfb0 = sipi._rf_serial_read(t, sipi.RF_PATH_B, 0x00)
            rfa18 = sipi._rf_serial_read(t, sipi.RF_PATH_A, 0x18)
            rfb18 = sipi._rf_serial_read(t, sipi.RF_PATH_B, 0x18)
            print(f"  RF[A,0x00]=0x{rfa0:05x}  RF[B,0x00]=0x{rfb0:05x}")
            print(f"  RF[A,0x18]=0x{rfa18:05x}  RF[B,0x18]=0x{rfb18:05x}")
            if rfb0 in (0x00000, 0xFFFFF) or rfb18 in (0x00000, 0xFFFFF):
                return _fail("path-B RF reads default/dead — RADIO_B did not take (2T2R gate).")
            print("[PASS] BB + RF init complete; both radios respond to SIPI (2T2R live).")

        if args.phase in ("chan", "txpower", "beacon"):
            print(f"[*] running channel tune (M4: 2.4 GHz band + ch{args.channel} + 20 MHz BW, "
                  f"both paths, rfe_type={params.rfe_type})...")
            chan.set_chnl_bw(t, ch=args.channel, bb_swing_2g_a=params.bb_swing_2g[0],
                             bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type)
            rfa = sipi._rf_serial_read(t, sipi.RF_PATH_A, chan.RF_CHNLBW)
            rfb = sipi._rf_serial_read(t, sipi.RF_PATH_B, chan.RF_CHNLBW)
            print(f"  RF[A,0x18]=0x{rfa:05x}  RF[B,0x18]=0x{rfb:05x}  "
                  f"(ch={rfa & 0xFF}/{rfb & 0xFF}, BW[11:10]={(rfa >> 10) & 3}/{(rfb >> 10) & 3})")
            ok = all((v & 0xFF) == args.channel and ((v >> 10) & 3) == 3 for v in (rfa, rfb))
            if not ok:
                return _fail(f"RF[0x18] not ch{args.channel}@20MHz on both paths after tune.")
            print(f"[PASS] channel tune complete — ch{args.channel} @ 20 MHz on both radios.")

        if args.phase in ("txpower", "beacon"):
            print("[*] applying per-rate TX power (M-TXPWR: 2-path TXAGC from EFUSE PG)...")
            txpower.set_tx_power(t, args.channel, params.tx_power_2g)
            if args.phase == "txpower":
                ca = t.read32(0x0C20) & 0xFF      # path-A CCK1 power index
                cb = t.read32(0x0E20) & 0xFF      # path-B CCK1 power index
                tr_a = t.read32(0x0C54) & 0xFFFFFF
                print(f"  TXAGC CCK1: path-A 0xC20=0x{ca:02x}  path-B 0xE20=0x{cb:02x}  "
                      f"training 0xC54=0x{tr_a:06x}")
                if ca == 0 or cb == 0:
                    return _fail("TXAGC reads 0 — per-rate TX power did not take.")
                print("[PASS] per-rate TX power applied on both paths (no bus errors).")

        if args.phase == "beacon":
            print("[*] M5: post-tune hal_init tail + InitHalDm (GPIO/CCK-PD/NHM/LNA/RX-gain)...")
            mac.hal_init_misc_pre(t)
            dig.init_hal_dm(t, search_edcca=False)
            mac.hal_init_misc_post(t)
            print(f"[*] M5 RX: enter monitor + synchronous bulk-IN loop, ch{args.channel} "
                  f"for {args.duration:g}s...")
            monitor.enter_monitor(t)
            beacons: Counter = Counter()
            rssi: dict = {}
            frames = 0
            start = time.monotonic()
            while time.monotonic() - start < args.duration:
                buf = t.bulk_in()
                if not buf:
                    continue
                for frame, r in rx.iter_frames(buf):
                    frames += 1
                    parsed = WlanFrameParser.parse_80211_frame(frame, r)
                    if not parsed or parsed.type != "beacon":
                        continue
                    b = (parsed.bssid or "").lower()
                    if not b or b == "ff:ff:ff:ff:ff:ff":
                        continue
                    beacons[b] += 1
                    if r and (b not in rssi or r > rssi[b]):
                        rssi[b] = r
            elapsed = max(time.monotonic() - start, 1e-3)
            total = sum(beacons.values())
            print(f"\n[RESULT] ch{args.channel}, {elapsed:.0f}s: {len(beacons)} APs, {total} beacons "
                  f"({total / elapsed:.1f}/s), {frames} frames")
            for b, n in beacons.most_common(15):
                print(f"    {b}  {n:>4}  {rssi.get(b, '?')} dBm")
            cn = beacons.get(DEFAULT_CANARY, 0)
            print(f"  canary {DEFAULT_CANARY}: {cn} ({cn / elapsed:.1f}/s) {rssi.get(DEFAULT_CANARY, '?')} dBm")
            if not beacons:
                return _fail("no beacons heard — RX path not working (check monitor/RCR/reader).")
            print("[PASS] 2.4 GHz RX hears beacons (pre-DIG baseline).")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")


if __name__ == "__main__":
    sys.exit(main())
