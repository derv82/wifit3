"""Throwaway: is phydm_dc_cancellation poisoning the RX path on live silicon?

The DC-cancellation (dm._dc_cancellation) measures the path-A DC offset off BB
dbg-port 0x200 (read at 0xfa0) and writes a computed compensation to 0xc10/0xc14
(shared path-A RX AFE/DC, OFDM + CCK). On the gate that measurement is the RECORDED
value, so the writes match byte-for-byte; on hardware it is measured fresh. A bad
measurement -> a corrupting compensation -> OFDM noise-flood + dead CCK (our symptom).

Modes:
  diag   : run real cold_bringup, capture OUR live measurement (0xfa0 @ dbg 0x200) and
           the resulting 0xc10/0xc14, print the vendor's RECORDED measurement + comp
           for comparison, then FA/tally ch1/ch6.
  skip   : monkeypatch _dc_cancellation to a no-op, cold_bringup, FA/tally ch1/ch6.
           RX recovering => the DC compensation was the poison.
  vendor : real cold_bringup, then overwrite 0xc10/0xc14 with the vendor's RECORDED
           values, FA/tally ch1/ch6.

Passive. Usage: uv run python scripts/rtl8821cu_dkms/dc_probe.py [diag|skip|vendor]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import libusb_package
import usb.core
import usb.util

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8821cu_dkms import bringup, chan
from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.bb import set_bb_reg
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"


def _vendor_dc() -> dict:
    """Pull the vendor's recorded DC measurement (0xfa0 read just before the 0xc10 comp) and the
    final 0xc10/0xc14 writes out of the cold-init capture."""
    dev = rp.find_card_device(DEFAULT_CAP)
    ctrl = rp.extract_ctrl_ops(DEFAULT_CAP, dev)
    fa0_reads, c10w, c14w, meas = [], [], [], None
    for o in ctrl:
        if o["dir"] == "IN" and o["wval"] == 0x0FA0 and o["data"]:
            fa0_reads.append((o["frame"], int.from_bytes(o["data"], "little")))
        if o["dir"] == "OUT" and o["wval"] == 0x0C10 and o["width"] == 4 and o["data"]:
            if meas is None and fa0_reads:
                meas = fa0_reads[-1][1]          # measurement latched just before 1st 0xc10 write
            c10w.append(int.from_bytes(o["data"], "little"))
        if o["dir"] == "OUT" and o["wval"] == 0x0C14 and o["width"] == 4 and o["data"]:
            c14w.append(int.from_bytes(o["data"], "little"))
    return {"meas": meas, "c10": c10w[-1] if c10w else None,
            "c14": c14w[-1] if c14w else None, "n_fa0": len(fa0_reads)}


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


def _reset_fa(t):
    set_bb_reg(t, 0x09A4, 1 << 17, 1); set_bb_reg(t, 0x09A4, 1 << 17, 0)
    set_bb_reg(t, 0x0A2C, 1 << 15, 0); set_bb_reg(t, 0x0A2C, 1 << 15, 1)
    set_bb_reg(t, 0x0B58, 1 << 0, 1); set_bb_reg(t, 0x0B58, 1 << 0, 0)


def _probe(t, info, ch: int):
    chan.set_channel(t, info, ch)
    _reset_fa(t)
    time.sleep(1.2)
    ofdm = t.read32(0x0F48) & 0xFFFF
    cck = t.read32(0x0A5C) & 0xFFFF
    good = c2h = 0
    end = time.monotonic() + 2.0
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
    print(f"  ch{ch:<3} ofdm_fa={ofdm:>6}  cck_fa={cck:>5}  good={good:>4}  c2h={c2h:>3}  "
          f"0xc10=0x{t.read32(0x0C10):08x} 0xc14=0x{t.read32(0x0C14):08x}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "diag"
    vend = _vendor_dc()
    print(f"vendor recorded DC: measurement(0xfa0)=0x{vend['meas']:05x}  "
          f"-> 0xc10=0x{vend['c10']:08x} 0xc14=0x{vend['c14']:08x}  "
          f"({vend['n_fa0']} total 0xfa0 reads)\n")

    captured = []
    if mode == "skip":
        dm_mod._dc_cancellation = lambda *a, **k: None      # noqa: SLF001
        print("[mode=skip] _dc_cancellation patched to no-op\n")
    else:
        _orig_get, _orig_set = dm_mod._get_bb_dbg_port_val, dm_mod._set_bb_dbg_port
        st = {"port": None}

        def _set_hook(t, port):
            st["port"] = port
            return _orig_set(t, port)

        def _get_hook(t):
            v = _orig_get(t)
            captured.append((st["port"], v))
            return v
        dm_mod._set_bb_dbg_port, dm_mod._get_bb_dbg_port_val = _set_hook, _get_hook

    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up...")
        info = bringup.cold_bringup(t)
        if mode != "skip":
            dc_meas = [v for p, v in captured if p == 0x200]
            print(f"[*] OUR live measurement (0xfa0 @ dbg 0x200): "
                  f"{[f'0x{v:05x}' for v in dc_meas]}")
            print(f"[*] OUR resulting 0xc10=0x{t.read32(0x0C10):08x} 0xc14=0x{t.read32(0x0C14):08x}")
            print(f"    vendor had 0xc10=0x{vend['c10']:08x} 0xc14=0x{vend['c14']:08x}")
        if mode == "vendor":
            t.write32(0x0C10, vend["c10"]); t.write32(0x0C14, vend["c14"])
            print(f"[mode=vendor] forced 0xc10/0xc14 to vendor's recorded values")
        print(f"\n[{mode}] FA/RX per channel:")
        for ch in (1, 6, 11):
            _probe(t, info, ch)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
