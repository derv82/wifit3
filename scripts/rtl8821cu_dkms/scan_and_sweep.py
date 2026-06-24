"""Throwaway: scan every channel for signal, then opportunistically sweep the crystal cap.

The 2.4 GHz band has been intermittent (strong AP bursts, then minutes of noise), so a
standalone crystal sweep keeps missing the signal. This does it in one pass: one
cold_bringup, scan all channels for beacon-headers (signal presence), and if the
busiest channel has sustained signal, immediately sweep the crystal cap there (while
the signal window is open) using a signal-gated per-cap measure. A good-frame peak
!= 0x2e => the per-unit crystal cap is the CFO fault. Robust: every bulk_in is guarded
so a transient USB error can't wedge the run; total runtime is bounded.

Passive (no TX).  uv run python -u scripts/rtl8821cu_dkms/scan_and_sweep.py
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
from wifit3.chips.rtl8821cu_dkms.bb import set_crystal_cap
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)
_CHANNELS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 36, 40, 44, 48, 149, 153, 157, 161]


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


def _rd(t):
    try:
        return t.bulk_in()
    except Exception:  # noqa: BLE001 - never let a USB hiccup wedge the sweep
        return None


def _full_hdr(m):
    return (len(m) >= 24 and m[0] == 0x80 and m[4:10] == b"\xff\xff\xff\xff\xff\xff"
            and m[10:16] == m[16:22] and m[10:16] != b"\x00" * 6)


def _walk(buf):
    off, n = 0, len(buf)
    while off + 24 <= n:
        d = query_rx_desc(buf[off:off + 24])
        if d.pkt_len <= 0:
            break
        po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if off + po > n:
            break
        if not d.rpt_sel:
            s = off + 24 + d.drvinfo_sz + d.shift_sz
            yield d, buf[s:s + d.pkt_len]
        off += (po + 7) & ~7


def _count(t, dwell):
    good = full = total = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        buf = _rd(t)
        if not buf:
            continue
        for d, m in _walk(buf):
            total += 1
            good += not (d.crc_err or d.icv_err)
            full += _full_hdr(m)
    return good, full, total


def main() -> int:
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...", flush=True)
        info = bringup.cold_bringup(t)
        print("[*] scanning all channels for signal (beacon-headers/1.2s):", flush=True)
        best = (None, -1)
        for ch in _CHANNELS:
            try:
                chan.set_channel(t, info, ch)
            except Exception as e:  # noqa: BLE001
                print(f"   ch{ch}: tune error {e}", flush=True)
                continue
            t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
            g, f, tot = _count(t, 1.2)
            print(f"   ch{ch:<3}: good={g:<3} hdrs={f:<4} total={tot}", flush=True)
            if f > best[1]:
                best = (ch, f)
        ch, nh = best
        print(f"\n[*] busiest: ch{ch} with {nh} hdrs/1.2s", flush=True)
        if nh < 12:
            print("[*] no sustained signal anywhere -> crystal sweep would be "
                  "inconclusive; skipping.", flush=True)
            return 0
        print(f"[*] sweeping crystal cap on ch{ch} (default 0x2e):", flush=True)
        chan.set_channel(t, info, ch)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        acc = {}
        for _ in range(3):
            for cap in range(0x24, 0x39):
                set_crystal_cap(t, cap)
                time.sleep(0.15)
                g, f, tot = _count(t, 0.9)
                pg, pf = acc.get(cap, (0, 0))
                acc[cap] = (pg + g, pf + f)
        print("\n  cap   good  hdrs", flush=True)
        bestcap = (-1, -1)
        for cap in sorted(acc):
            g, f = acc[cap]
            mark = " <-default" if cap == 0x2E else ""
            if g > bestcap[1]:
                bestcap = (cap, g)
            print(f"  0x{cap:02x}  {g:>4}  {f:>4}{mark}", flush=True)
        print(f"\n  most good frames at cap 0x{bestcap[0]:02x} ({bestcap[1]}); default 0x2e",
              flush=True)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
