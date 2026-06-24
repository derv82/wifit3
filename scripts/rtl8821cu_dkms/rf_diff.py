"""Throwaway: RF (radio) register final-state diff, live silicon vs vendor pcap.

live_write_diff collapsed every RF write into one 0xc90 LSSI entry (last-write-wins),
so it only ever compared the LAST RF write. RF writes carry the RF address in the LSSI
word ((addr[7:0]<<20)|data[19:0]); RF RMW writes (write_rf_masked) read the live RF
value first, so they are exactly the live-read-computed kind that diverge on our card
yet pass the gate. This decodes every 0xc90 write's RF address and diffs the final
per-RF-register state -- catching radio mistunes the BB-register diff cannot see.

Passive (cold init only). uv run python scripts/rtl8821cu_dkms/rf_diff.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import libusb_package
import usb.core
import usb.util

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8821cu_dkms import bringup
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"
_LSSI_A = 0x0C90                # path-A RF LSSI write port
_COLD_END_FRAME = 17019


def _decode(v: int) -> tuple[int, int]:
    return (v >> 20) & 0xFF, v & 0xFFFFF      # rf_addr, rf_data


class LoggingTransport(Rtl8821cuTransport):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rf: dict[int, int] = {}          # rf_addr -> last rf_data written

    def writeN(self, addr, data):
        data = bytes(data)
        if addr == _LSSI_A and len(data) == 4:
            ra, rd = _decode(int.from_bytes(data, "little"))
            self.rf[ra] = rd
        super().writeN(addr, data)


def _pcap_rf():
    dev = rp.find_card_device(DEFAULT_CAP)
    ctrl = rp.extract_ctrl_ops(DEFAULT_CAP, dev)
    rf: dict[int, int] = {}
    for o in ctrl:
        if (o["dir"] == "OUT" and o["wval"] == _LSSI_A and o["width"] == 4
                and o["data"] and o["frame"] <= _COLD_END_FRAME):
            ra, rd = _decode(int.from_bytes(o["data"], "little"))
            rf[ra] = rd
    return rf


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


def main() -> int:
    pcap_rf = _pcap_rf()
    dev, intf = _open()
    t = LoggingTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up on live hardware (logging RF writes)...")
        bringup.cold_bringup(t)
        live_rf = t.rf
        print(f"[*] live wrote {len(live_rf)} distinct RF regs; vendor cold wrote "
              f"{len(pcap_rf)}\n")
        addrs = sorted(set(live_rf) | set(pcap_rf))
        diffs = 0
        for a in addrs:
            lv = live_rf.get(a)
            pv = pcap_rf.get(a)
            if lv != pv:
                diffs += 1
                ls = f"0x{lv:05x}" if lv is not None else "(unwritten)"
                ps = f"0x{pv:05x}" if pv is not None else "(unwritten)"
                xor = (lv ^ pv) if (lv is not None and pv is not None) else 0
                print(f"  RF 0x{a:02x}: live={ls:>9}  vendor={ps:>9}  xor=0x{xor:05x}")
        print(f"\n{diffs} RF register divergences.")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
