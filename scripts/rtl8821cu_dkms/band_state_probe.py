"""Hardware probe (passive, RX only -- no TX): is the 2.4 GHz RX dead cold, does a 5 GHz
bounce change it, and does the chip's band-state differ between cold-ch1 and ch1-after-5G?

Runs cold_bringup (ends on ch1, monitor), then A) measures ch1 cold, B) tunes ch36 (5G) and
measures, C) tunes back to ch1 and measures. Per phase it tallies good beacons / CRC-err /
beacon-header-correct frames, and it snapshots the band-fingerprint registers so the A-vs-C
diff shows whether anything 5 GHz is left "cached" after a cold ch1 tune.

    uv run python scripts/rtl8821cu_dkms/band_state_probe.py [secsA] [secsB] [secsC]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, chan
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF
_REG_RCR = 0x0608
_RCR_ACCEPT_ERR = 0x90000001 | (1 << 8) | (1 << 9)   # + ACRC32 + AICV: deliver crc-err frames too


def _open():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        raise SystemExit("no 0bda:c820 (WiFi mode) device -- ZeroCD? replug/mode-switch first")
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
    usb.util.claim_interface(dev, intf)
    return dev, intf


def read_band_regs(t) -> dict:
    """The 'what band is the radio really in' fingerprint."""
    rf18 = read_rf(t, 0x18)
    cb8 = t.read32(0x0CB8)
    a80 = t.read32(0x0A80)
    c1c = t.read32(0x0C1C)
    return {
        "RF18": rf18,
        "RF18.ch": rf18 & 0xFF,
        "RF18.5Gbit16": (rf18 >> 16) & 1,
        "RF18.5Gbit8": (rf18 >> 8) & 1,
        "RF18.bw11_10": (rf18 >> 10) & 3,
        "RFdf.tanklut6": (read_rf(t, 0xDF) >> 6) & 1,
        "0xCB8.rfset": cb8,
        "0x808.cck28": (t.read32(0x0808) >> 28) & 1,
        "0xA80.bbcck18": (a80 >> 18) & 1,
        "0xA80.gain15_0": a80 & 0xFFFF,
        "0xA84.23_16": (t.read32(0x0A84) >> 16) & 0xFF,
        "0xC1C.agc11_8": (c1c >> 8) & 0xF,
        "0xC1C.swing": (c1c >> 21) & 0x7FF,
        "0x860.fc": (t.read32(0x0860) >> 17) & 0xFFF,
        "0x8AC.bw": t.read32(0x08AC),
        "0xC50.IGI": t.read8(0x0C50) & 0x7F,
        "RCR": t.read32(0x0608),
    }


def _hdr_score(m: bytes) -> int:
    """Beacon-header invariants (0..6): FC=0x80, FC[1]=0, addr1=bcast, addr2==addr3,
    addr2 not bcast, addr2 not zero. 6 => first 22 bytes demodulated correctly."""
    if len(m) < 24:
        return 0
    return (int(m[0] == 0x80) + int(m[1] == 0x00)
            + int(m[4:10] == b"\xff\xff\xff\xff\xff\xff")
            + int(m[10:16] == m[16:22])
            + int(m[10:16] != b"\xff\xff\xff\xff\xff\xff")
            + int(m[10:16] != b"\x00\x00\x00\x00\x00\x00"))


def measure(t, secs: float) -> dict:
    total = crc_err = crc_ok = fc80 = good_bcn = hdr_ok = hdr_ok_crcfail = 0
    score_hist = Counter()
    bssids = set()
    end = time.monotonic() + secs
    while time.monotonic() < end:
        buf = t.bulk_in()
        if not buf:
            continue
        off, nb = 0, len(buf)
        while off + 24 <= nb:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            po = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + po > nb:
                break
            if not d.rpt_sel:
                total += 1
                crc_err += d.crc_err
                crc_ok += not d.crc_err
                start = off + 24 + d.drvinfo_sz + d.shift_sz
                m = buf[start:start + d.pkt_len]
                if m and m[0] == 0x80:
                    fc80 += 1
                sc = _hdr_score(m)
                score_hist[sc] += 1
                if sc == 6:
                    hdr_ok += 1
                    if d.crc_err:
                        hdr_ok_crcfail += 1
                    else:
                        good_bcn += 1
                        bssids.add(bytes(m[10:16]))
            off += (po + 7) & ~7
    return dict(total=total, crc_ok=crc_ok, crc_err=crc_err, fc80=fc80, good_bcn=good_bcn,
                hdr_ok=hdr_ok, hdr_ok_crcfail=hdr_ok_crcfail, uniq=len(bssids), secs=secs,
                hist={k: score_hist.get(k, 0) for k in range(7)})


def _pr_regs(label, regs):
    print(f"  [{label}]")
    for k, v in regs.items():
        print(f"    {k:16s} = {hex(v)}")


def main() -> int:
    sA = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    sB = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    sC = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    dev, intf = _open()
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        print("[*] cold bring-up (ends on ch1, monitor)...")
        info = bringup.cold_bringup(t)
        regs_a = read_band_regs(t)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        print(f"[*] PHASE A: cold ch1, {sA:.0f}s ...")
        a = measure(t, sA)
        print(f"[*] PHASE B: tune ch36 (5G), {sB:.0f}s ...")
        chan.set_channel(t, info, 36)
        regs_b = read_band_regs(t)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        b = measure(t, sB)
        print(f"[*] PHASE C: tune back ch1, {sC:.0f}s ...")
        chan.set_channel(t, info, 1)
        regs_c = read_band_regs(t)
        t.write32(_REG_RCR, _RCR_ACCEPT_ERR)
        c = measure(t, sC)

        print("\n==== BEACON / FRAME TALLIES (RCR widened to accept crc-err) ====")
        for label, r in [("A cold-ch1", a), ("B ch36(5G)", b), ("C ch1-after5G", c)]:
            print(f"  {label:14s}: good_bcn={r['good_bcn']:4d} ({r['good_bcn']/r['secs']:.1f}/s) "
                  f"uniq={r['uniq']:3d} | hdrOK={r['hdr_ok']:4d} hdrOK_crcFAIL={r['hdr_ok_crcfail']:4d} "
                  f"| total={r['total']:5d} crc_ok={r['crc_ok']:5d} crc_err={r['crc_err']:5d} fc80={r['fc80']:4d}")
        print("  (hdrOK_crcFAIL high vs good_bcn => correct headers, errors over length => marginal EVM/analog)")

        print("\n==== BAND-STATE REGISTERS ====")
        _pr_regs("A cold-ch1", regs_a)
        _pr_regs("B ch36(5G)", regs_b)
        _pr_regs("C ch1-after5G", regs_c)
        print("\n==== A vs C diff (cold-ch1 vs ch1-after-5G -- the 'cached 5G state' hypothesis) ====")
        diff = [k for k in regs_a if regs_a[k] != regs_c[k]]
        if diff:
            for k in diff:
                print(f"    DIFF {k:16s}: A={hex(regs_a[k])}  C={hex(regs_c[k])}")
        else:
            print("    (none -- cold-ch1 and ch1-after-5G are register-identical)")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
