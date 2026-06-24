"""Throwaway: confirm the 2.4 GHz fix — RF 0xb2 vs a full 5 GHz->2.4 GHz band switch.

Reproducible finding: after cold init the 2.4 GHz RX is DEAD (0 good frames) until a
5 GHz->2.4 GHz band switch occurs, after which it works (the user's "flip" idea).
flip_compare found the only differing register is RF 0xb2 (0x22473 cold vs 0x224b0
after the flip). This isolates the cause:
  A,B: cold-direct ch6 (expect dead, twice -- rule out warm-up)
  C:   write RF 0xb2 = 0x224b0, re-measure (RF 0xb2 alone the fix?)
  D:   full band switch ch6->ch149(5G)->ch6, re-measure (band switch the fix?)

Passive.  uv run python -u scripts/rtl8821cu_dkms/rf_b2_fix_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chan
from wifit3.chips.rtl8821cu_dkms.rf import read_rf, write_rf
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


def _full(m):
    return (len(m) >= 24 and m[0] == 0x80 and m[4:10] == b"\xff\xff\xff\xff\xff\xff"
            and m[10:16] == m[16:22] and m[10:16] != b"\x00" * 6)


def _measure(t, dwell):
    good = full = total = 0
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
                s = off + 24 + d.drvinfo_sz + d.shift_sz
                full += _full(buf[s:s + d.pkt_len])
            off += (po + 7) & ~7
    return good, full, total


def _row(t, label):
    g, f, tot = _measure(t, 3.0)
    b2 = read_rf(t, 0xB2) & 0xFFFFF
    print(f"  {label:<34} RF0xb2=0x{b2:05x}  good={g:<4} hdr={f:<4} total={tot}", flush=True)
    return g


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up (ch1 2.4 GHz direct, never visits 5 GHz)...", flush=True)
        info = bringup.cold_bringup(t)
        chan.set_channel(t, info, 6)            # 2.4G, same-band, no switch
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        _row(t, "[A] cold-direct ch6")
        _row(t, "[B] cold-direct ch6 (again)")
        write_rf(t, 0xB2, 0x224B0)              # the post-flip value
        gc = _row(t, "[C] after write RF 0xb2=0x224b0")
        chan.set_channel(t, info, 149)          # full band switch to 5G...
        chan.set_channel(t, info, 6)            # ...and back to 2.4G
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        gd = _row(t, "[D] after band switch ch6->149->6")
        print("\n  VERDICT:", flush=True)
        if gc >= 20:
            print("   writing RF 0xb2 alone repairs 2.4 GHz => RF 0xb2 is the fix.", flush=True)
        elif gd >= 20:
            print("   RF 0xb2 alone did NOT fix it but the band switch did => the fix is the full "
                  "band-switch sequence (PLL relock / more than RF 0xb2).", flush=True)
        else:
            print("   neither repaired it this window (signal may have dropped); re-run.", flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
