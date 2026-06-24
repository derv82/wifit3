"""Throwaway: controlled test — is GNT_WL=SW_HIGH what makes WiFi RX work (BT uninit)?

5 GHz RX works; 2.4 GHz fails. The only band difference in the antenna setup is the
WiFi GNT: PHASE_5G forces GNT_WL=SW_HIGH (WiFi always granted) while PHASE_2G leaves
GNT_WL=HW_PTA (arbitrated by the BT coprocessor, which wifit3 never initializes). If
the uninitialized BT side never grants the 2.4 GHz antenna to WiFi, that explains the
split exactly.

Test it on the band that HAS signal: tune a strong 5 GHz channel (works), then drop
GNT_WL to HW_PTA (the 2.4 GHz setting) and re-measure. If 5 GHz RX collapses, GNT_WL=
SW_HIGH is the load-bearing setting -> the 2.4 GHz fix is to force the same. Restores
SW_HIGH at the end. Passive.
    uv run python -u scripts/rtl8821cu_dkms/gnt_5g_test.py [5g_channel]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, btc, chan
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)


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


def _measure(t, dwell):
    good = total = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        try:
            buf = t.bulk_in()
        except Exception:  # noqa: BLE001
            buf = None
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
            if not d.rpt_sel:
                total += 1
                good += not (d.crc_err or d.icv_err)
            off += (po + 7) & ~7
    return good, total


def _gnt38(t):
    return btc._read_indirect(t, 0x38)


def main() -> int:
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 157
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up; scanning 5 GHz for the busiest channel...", flush=True)
        info = bringup.cold_bringup(t)
        best = (ch, -1)
        for c in (36, 40, 44, 48, 149, 153, 157, 161):
            chan.set_channel(t, info, c)
            t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
            g, tot = _measure(t, 0.9)
            print(f"   ch{c}: good={g} total={tot}", flush=True)
            if g > best[1]:
                best = (c, g)
        ch = best[0]
        print(f"[*] testing GNT on 5 GHz ch{ch} (busiest, {best[1]} good)...", flush=True)
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)

        print(f"  [A] GNT_WL=SW_HIGH (default 5G)  ltecoex0x38=0x{_gnt38(t):08x}", flush=True)
        g, tot = _measure(t, 3.0)
        print(f"      -> good={g}  total={tot}", flush=True)

        btc._set_gnt_wl(t, btc._GNT_HW_PTA)            # drop to the 2.4 GHz setting
        print(f"\n  [B] GNT_WL=HW_PTA (the 2.4G setting)  ltecoex0x38=0x{_gnt38(t):08x}", flush=True)
        g2, tot2 = _measure(t, 3.0)
        print(f"      -> good={g2}  total={tot2}", flush=True)

        btc._set_gnt_wl(t, btc._GNT_SW_HIGH)           # restore
        print(f"\n  [C] GNT_WL=SW_HIGH (restored)  ltecoex0x38=0x{_gnt38(t):08x}", flush=True)
        g3, tot3 = _measure(t, 3.0)
        print(f"      -> good={g3}  total={tot3}", flush=True)

        print("\n  VERDICT:", flush=True)
        if g >= 10 and g2 == 0 and g3 >= 10:
            print("   GNT_WL=SW_HIGH is load-bearing: HW_PTA collapses 5G RX and SW_HIGH restores it."
                  "\n   => 2.4 GHz fails because PHASE_2G uses HW_PTA; the fix is to force GNT_WL="
                  "SW_HIGH on 2.4 GHz too (BT coprocessor is uninitialized).", flush=True)
        elif g >= 10 and g2 >= 10:
            print("   5G RX survives HW_PTA => GNT_WL is NOT the differentiator; the 2.4 GHz fault is "
                  "elsewhere (BTG LNA / 2.4G RF path).", flush=True)
        else:
            print("   inconclusive (insufficient 5G signal during the window; re-run).", flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
