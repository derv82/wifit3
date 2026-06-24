"""Throwaway: is the BB receiving RF energy at all, and is it OFDM-only or also CCK?

The clean monitor config reads back identical to the vendor's receiving state yet
delivers 0 MPDUs. C2H (firmware->FIFO->host) works, so the dead link is RF->BB->FIFO.
This splits that: per channel, reset the phydm false-alarm counters, dwell, and read
OFDM-FA (0xf48) vs CCK-FA (0xa5c) + the page-F CCA/CRC counters + IGI. FA moving =>
the BB demod sees energy on that path; flat zero => that path is deaf. A 1 Mbps AP
beacons in CCK, so an OFDM-alive / CCK-dead split explains "no beacons".

Passive: tunes channels (RF/BB writes + coex H2C, no 802.11 TX) and reads counters.
    uv run python scripts/rtl8821cu_dkms/rx_energy.py
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
from wifit3.chips.rtl8821cu_dkms.bb import set_bb_reg
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF

_REG_OFDM_FA = 0x0F48
_REG_CCK_FA = 0x0A5C
_REG_IGI = 0x0C50
_REG_CCK_CCA = 0x0FCC            # cnt_cck_cca low word region (page-F)
_REG_OFDM_CCA = 0x0F08
_CHANNELS = [1, 6, 11, 36, 149]


def _open():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] Wi-Fi {USB_VID:04x}:{USB_PID:04x} not found")
        return None, None
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
    usb.util.claim_interface(dev, intf)
    return dev, intf


def _reset_fa(t) -> None:
    """phydm_false_alarm_counter_reg_reset (11AC) — OFDM 0x9a4[17], CCK 0xa2c[15], page-F 0xb58[0]."""
    set_bb_reg(t, 0x09A4, 1 << 17, 1)
    set_bb_reg(t, 0x09A4, 1 << 17, 0)
    set_bb_reg(t, 0x0A2C, 1 << 15, 0)
    set_bb_reg(t, 0x0A2C, 1 << 15, 1)
    set_bb_reg(t, 0x0B58, 1 << 0, 1)
    set_bb_reg(t, 0x0B58, 1 << 0, 0)


def _sample(t, dwell: float) -> dict:
    _reset_fa(t)
    time.sleep(dwell)
    return {
        "ofdm_fa": t.read32(_REG_OFDM_FA) & 0xFFFF,
        "cck_fa": t.read32(_REG_CCK_FA) & 0xFFFF,
        "ofdm_cca": t.read32(_REG_OFDM_CCA) & 0xFFFF,
        "cck_cca": t.read32(_REG_CCK_CCA) & 0xFFFF,
        "igi": t.read32(_REG_IGI) & 0x7F,
    }


def _tally(t, dwell: float) -> tuple[int, int, int]:
    good = c2h = bufs = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        buf = t.bulk_in()
        if not buf:
            continue
        bufs += 1
        off, n = 0, len(buf)
        while off + 24 <= n:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + po > n:
                break
            if d.rpt_sel:
                c2h += 1
            elif not (d.crc_err or d.icv_err):
                good += 1
            off += (po + 7) & ~7
    return bufs, good, c2h


def main() -> int:
    dev, intf = _open()
    if dev is None:
        return 1
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...")
        info = bringup.cold_bringup(t)
        print(f"[*] cold init OK (rfe=0x{info.rfe_type:x}, thermal={info.eeprom_thermal})\n")
        print("per-channel RF-energy probe (FA/CCA deltas over 1.2s windows, counters reset each):")
        print("  ch    rf18      ofdm_fa  cck_fa  ofdm_cca  cck_cca  igi   |  bulk good c2h")
        for ch in _CHANNELS:
            chan.set_channel(t, info, ch)
            rf18 = read_rf(t, 0x18) & 0xFFFFF
            # two FA windows
            s = _sample(t, 1.2)
            bufs, good, c2h = _tally(t, 2.0)
            print(f"  {ch:<4}  0x{rf18:05x}   {s['ofdm_fa']:>7}  {s['cck_fa']:>6}  "
                  f"{s['ofdm_cca']:>8}  {s['cck_cca']:>7}  0x{s['igi']:02x}  |  "
                  f"{bufs:>4} {good:>4} {c2h:>3}")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
