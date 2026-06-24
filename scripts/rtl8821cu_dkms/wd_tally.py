"""Throwaway: does the real driver behavior (phydm watchdog / DIG running) unblock RX?

rx_diag/rx_energy tallied with NO watchdog, so DIG never adapted IGI against the FA
flood. The product connect() runs the watchdog every 2s. This replicates that: per
channel, run N watchdog ticks (printing IGI + OFDM/CCK-FA so we see DIG climb and FA
fall), then tally bulk-IN. If real frames need DIG to suppress the FA flood first,
they appear here; if not, this confirms the receiver is deaf independent of DIG.

Passive. uv run python scripts/rtl8821cu_dkms/wd_tally.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chan, efuse, watchdog
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

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


def _tally(t, dwell):
    good = c2h = 0
    end = time.monotonic() + dwell
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
            c2h += 1 if d.rpt_sel else 0
            good += 1 if not (d.rpt_sel or d.crc_err or d.icv_err) else 0
            off += (po + 7) & ~7
    return good, c2h


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...")
        info = bringup.cold_bringup(t)
        st = watchdog.WatchdogState(eeprom_thermal=info.eeprom_thermal,
                                    thermal_offset=efuse.thermal_offset(info))
        for ch in (1, 6):
            chan.set_channel(t, info, ch)
            print(f"\n=== ch{ch}: watchdog ticks (DIG vs FA flood) ===")
            for i in range(10):
                watchdog.tick(t, st)
                igi = t.read32(0x0C50) & 0x7F
                ofdm = t.read32(0x0F48) & 0xFFFF
                cck = t.read32(0x0A5C) & 0xFFFF
                print(f"  tick{i+1:>2}: IGI=0x{igi:02x}  ofdm_fa={ofdm:>6}  cck_fa={cck:>5}  "
                      f"cck_pd_lv={st.cck_pd_lv}")
                time.sleep(0.4)
            good, c2h = _tally(t, 4.0)
            print(f"  -> tally after DIG: good_80211={good}  c2h={c2h}")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
