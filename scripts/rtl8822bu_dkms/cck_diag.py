"""RTL8822BU (DKMS port) — CCK-vs-OFDM RX diagnostic for the 2.4 GHz CCK-starvation bug.

Passive: cold init + monitor enable + bulk-IN RX only. No 802.11 TX.

The 2.4 GHz CCK bug: OFDM beacons capture at ~8/s, CCK-1M beacons at ~2/s. test_hw counts
beacons but not by rate; --rxstats counts crc/icv but not by rate. This tool splits the bulk-IN
stream by RX rate (rxdesc+0x0C[6:0]; rate<4 = CCK) AND by descriptor category (good/crc_err/
icv_err), which decides the root cause:

  - few CCK packets of ANY category  -> CCK packet-detection / CCA never triggers (0xA0A cck_pd,
    CCK-enable 0x808[28], CCK CCA). --cckpd 0x40 (sensitive LV_0) should move it.
  - many CCK packets but crc_err     -> BB demods CCK but the bits are corrupt: CCK AGC table /
    new-CCK-AGC 0xA9C[17] / DC-cancel / CCK BB cal. --cckpd will NOT move it.

Register overrides (applied after each tune, before the dwell):
  --cckpd 0xVV         force CCK PD threshold 0xA0A (0x40 sensitive .. 0x83 LV_1 seed)
  --set ADDR=VAL[:SZ]  force an arbitrary reg (SZ = 1/2/4 bytes, default 4); repeatable.
                       e.g. --set 0xA9C=0x...:4   --set 0x808=...:4

Usage (card plugged in, WinUSB-bound):
    uv run python scripts/rtl8822bu_dkms/cck_diag.py --channel 1 --dwell 20
    uv run python scripts/rtl8822bu_dkms/cck_diag.py --channel 1 --dwell 20 --cckpd 0x40
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8822bu_dkms import bringup, chan, chipid, dm_watchdog, mac, rx, sipi
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport
from wifit3.wlan.packet import WlanFrameParser

USB_VID, USB_PID = 0x2357, 0x0138

# RX rate index [SRC] halmac_rx_desc_nic.h:392 GET_RX_DESC_RX_RATE = rxdesc+0x0C[6:0].
# 0..3 = CCK (1/2/5.5/11M); 4..11 = OFDM (6..54M); 12+ = HT/VHT MCS.
_RATE_NAME = {0: "CCK-1M", 1: "CCK-2M", 2: "CCK-5.5M", 3: "CCK-11M",
              4: "OFDM-6M", 5: "OFDM-9M", 6: "OFDM-12M", 7: "OFDM-18M",
              8: "OFDM-24M", 9: "OFDM-36M", 10: "OFDM-48M", 11: "OFDM-54M"}


def _rate_name(r: int) -> str:
    return _RATE_NAME.get(r, f"HT/VHT-mcs{r - 12}" if r >= 12 else f"rate{r}")


def _is_cck(r: int) -> bool:
    return r < 4


def _open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] RTL8822BU not found ({USB_VID:04x}:{USB_PID:04x}).")
        return None
    print(f"[*] Found RTL8822BU at bus {dev.bus}, address {dev.address}")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    return dev


def _rnd8(x):
    return (x + 7) & ~7


class Tally:
    """Per-rate breakdown of one dwell window."""

    def __init__(self):
        # rate_idx -> {good, crc_err, icv_err, c2h}
        self.by_rate = defaultdict(lambda: defaultdict(int))
        # bssid -> {rate_idx -> beacon_count}, and bssid -> best rssi
        self.beacons = defaultdict(lambda: defaultdict(int))
        self.rssi = {}
        self.bcn_int = {}                          # bssid -> beacon interval (TU) from the body
        self.bufs = self.nbytes = 0
        # per-second buckets: sec -> {"ofdm":n, "cck":n, bssid->cck_beacons}
        self.sec0 = None
        self.buckets = defaultdict(lambda: defaultdict(int))

    def walk(self, buf: bytes, now: float | None = None):
        if now is not None and self.sec0 is None:
            self.sec0 = now
        sec = int(now - self.sec0) if now is not None else -1
        self.bufs += 1
        self.nbytes += len(buf)
        off, n = 0, len(buf)
        while off + rx.RXDESC_SIZE <= n:
            w0 = int.from_bytes(buf[off:off + 4], "little")
            pkt_len = w0 & 0x3FFF
            crc_err = (w0 >> 14) & 1
            icv_err = (w0 >> 15) & 1
            drvinfo_sz = ((w0 >> 16) & 0xF) << 3
            shift_sz = (w0 >> 24) & 0x3
            physt = (w0 >> 26) & 1
            w3 = int.from_bytes(buf[off + 12:off + 16], "little")
            rate = w3 & 0x7F
            c2h = (int.from_bytes(buf[off + 8:off + 12], "little") >> 28) & 1
            if pkt_len <= 0:
                break
            pkt_offset = rx.RXDESC_SIZE + drvinfo_sz + shift_sz + pkt_len
            if pkt_offset > n - off:
                break
            if c2h:
                self.by_rate[rate]["c2h"] += 1
            elif crc_err:
                self.by_rate[rate]["crc_err"] += 1
            elif icv_err:
                self.by_rate[rate]["icv_err"] += 1
            elif pkt_len > rx.FCS_LEN:
                self.by_rate[rate]["good"] += 1
                if sec >= 0:
                    self.buckets[sec]["cck" if _is_cck(rate) else "ofdm"] += 1
                start = off + rx.RXDESC_SIZE + drvinfo_sz + shift_sz
                mpdu = buf[start:start + pkt_len - rx.FCS_LEN]
                rssi = rx._decode_rssi(buf[off + rx.RXDESC_SIZE:start]) if physt else None
                parsed = WlanFrameParser.parse_80211_frame(mpdu, rssi)
                if parsed and parsed.type == "beacon":
                    b = (parsed.bssid or "").lower()
                    if b and b != "ff:ff:ff:ff:ff:ff":
                        self.beacons[b][rate] += 1
                        # After the 24B MAC header: timestamp(8) | beacon-interval(2 TU) | cap(2).
                        if len(mpdu) >= 34 and b not in self.bcn_int:
                            self.bcn_int[b] = int.from_bytes(mpdu[32:34], "little")
                        if sec >= 0 and _is_cck(rate):
                            self.buckets[sec][b] += 1
                        if rssi is not None and (b not in self.rssi or rssi > self.rssi[b]):
                            self.rssi[b] = rssi
            off += _rnd8(pkt_offset)

    def report(self, channel, dwell):
        print(f"\n[CCK-DIAG] ch {channel}, {dwell:g}s: {self.bufs} bufs / {self.nbytes} B")
        print("\n  per-RATE descriptor categories (all frames, not just beacons):")
        print(f"    {'rate':<14} {'good':>6} {'crc_err':>8} {'icv_err':>8} {'c2h':>6}")
        cck_tot = ofdm_tot = 0
        for rate in sorted(self.by_rate):
            c = self.by_rate[rate]
            tot = c["good"] + c["crc_err"] + c["icv_err"]
            if _is_cck(rate):
                cck_tot += tot
            elif rate < 12 or rate >= 12:
                ofdm_tot += tot
            print(f"    {_rate_name(rate):<14} {c['good']:>6} {c['crc_err']:>8} "
                  f"{c['icv_err']:>8} {c['c2h']:>6}")
        print(f"\n  CCK frames (rate<4) total (good+crc+icv): {cck_tot}")
        print(f"  OFDM/HT frames total (good+crc+icv):      {ofdm_tot}")

        print("\n  per-BSSID beacons: measured rate vs the AP's advertised beacon interval "
              "(capture% = how many of its beacons we actually caught):")
        rows = []
        for b, perrate in self.beacons.items():
            cck = sum(n for r, n in perrate.items() if _is_cck(r))
            ofdm = sum(n for r, n in perrate.items() if not _is_cck(r))
            rates = "+".join(_rate_name(r) for r in sorted(perrate))
            rows.append((cck + ofdm, b, cck, ofdm, rates))
        rows.sort(reverse=True)
        hdr = (f"    {'bssid':<18} {'tot':>4} {'CCK':>4} {'OFDM':>4} {'rssi':>5} "
               f"{'int(TU)':>7} {'meas/s':>6} {'exp/s':>5} {'capt%':>5}  rates")
        print(hdr)
        for tot, b, cck, ofdm, rates in rows:
            ti = self.bcn_int.get(b)
            meas = tot / dwell
            exp = (1000.0 / (ti * 1.024)) if ti else None
            capt = (100.0 * meas / exp) if exp else None
            print(f"    {b:<18} {tot:>4} {cck:>4} {ofdm:>4} {self.rssi.get(b, '?'):>5} "
                  f"{(ti if ti else '?'):>7} {meas:>6.1f} "
                  f"{(f'{exp:.1f}' if exp else '?'):>5} {(f'{capt:.0f}' if capt else '?'):>5}  {rates}")

        if self.buckets and rows:
            top = rows[0][1]                       # strongest CCK-beaconing AP overall
            print(f"\n  per-second airtime correlation (top CCK AP {top}):")
            print(f"    {'sec':>3} {'ofdm/s':>7} {'cck/s':>6} {'topAP-bcn/s':>12}")
            for s in sorted(self.buckets):
                bk = self.buckets[s]
                print(f"    {s:>3} {bk.get('ofdm', 0):>7} {bk.get('cck', 0):>6} {bk.get(top, 0):>12}")
        return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--dwell", type=float, default=20.0)
    ap.add_argument("--scan", action="store_true",
                    help="hop 1-13 (--dwell s each) and report per-channel OFDM load + top CCK AP, "
                         "to find a quiet channel with a strong CCK beacon for a clean measurement")
    ap.add_argument("--cckpd", default=None, help="force 0xA0A CCK PD threshold (hex)")
    ap.add_argument("--igi", default=None,
                    help="force RX gain IGI (0xC50/0xE50[6:0]) to a hex value after tune; lower = "
                         "more sensitive. Tests whether the frozen dig_init IGI seed is too high.")
    ap.add_argument("--set", action="append", default=[], metavar="ADDR=VAL[:SZ]",
                    help="force an arbitrary reg after tune (SZ bytes 1/2/4, default 4); repeatable")
    ap.add_argument("--rcr", default=None, help="override monitor RCR (hex) after enable_monitor")
    ap.add_argument("--bssid", default=None,
                    help="focus AP for the per-second / sweep report (default: the top CCK AP)")
    ap.add_argument("--igisweep", default=None,
                    help="comma list of IGI hex values to A/B in ONE run (same tune/environment), e.g. "
                         "0x20,0x18,0x10,0x08. Splits --dwell across them; per IGI reports the focus "
                         "AP's beacons/s + zero-seconds. Finds the gain that hits 8-10/s, no zero-secs.")
    ap.add_argument("--watchdog", action="store_true",
                    help="run the ported PHYDM watchdog every ~2s during the dwell (live IGI/CCK-PD "
                         "adaptation) and report the IGI it converges to — tests whether our DIG "
                         "drives gain toward sensitive (like the vendor) or the wrong way.")
    args = ap.parse_args()

    dev = _open_device()
    if dev is None:
        return 1
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        print(f"[FAIL] claim_interface(0): {e}  (a running wifit3 may hold the card)")
        return 1

    t = Rtl8822buTransport(dev)
    try:
        info = chipid.get_chip_info(t)
        print(f"  chip_ver (cut) = {info.chip_ver}")
        print("[*] cold bring-up...")
        bringup.cold_bringup(t)
        print("[*] enable monitor + tune...")
        mac.enable_monitor(t)
        if args.rcr is not None:
            t.write32(0x0608, int(args.rcr, 0))

        if args.scan:
            prev = None
            print(f"\n  ch  {'ofdm':>7} {'cck':>5}  top-CCK-AP (beacons @ rssi)")
            for ch in range(1, 14):
                chan.set_channel_bw(t, ch, prev_ch=prev)
                prev = ch
                tally = Tally()
                start = time.monotonic()
                while True:
                    now = time.monotonic()
                    if now - start >= args.dwell:
                        break
                    buf = t.bulk_in()
                    if buf:
                        tally.walk(buf, now)
                ofdm = sum(c["good"] for r, c in tally.by_rate.items() if not _is_cck(r))
                cck = sum(c["good"] for r, c in tally.by_rate.items() if _is_cck(r))
                best = max(((sum(n for rr, n in pr.items() if _is_cck(rr)), b)
                            for b, pr in tally.beacons.items()), default=(0, "-"))
                bcount, bbss = best
                rs = tally.rssi.get(bbss, "?")
                print(f"  {ch:>2}  {ofdm:>7} {cck:>5}  {bbss} ({bcount} @ {rs} dBm)")
            return 0

        chan.set_channel_bw(t, args.channel, prev_ch=None)

        # Report the live CCK-path register state BEFORE any override.
        cck_en = (t.read32(0x0808) >> 28) & 1
        a0a = t.read8(0x0A0A)
        new_agc = (t.read32(0x0A9C) >> 17) & 1
        igi_a = t.read32(0x0C50) & 0x7F
        igi_b = t.read32(0x0E50) & 0x7F
        rf18a = sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18)
        print(f"  [regs] CCK-enable 0x808[28]={cck_en}  CCK-PD 0xA0A=0x{a0a:02x}  "
              f"new-CCK-AGC 0xA9C[17]={new_agc}  IGI 0xC50=0x{igi_a:02x}/0xE50=0x{igi_b:02x}  "
              f"RF18_A=0x{rf18a:05x}(ch={rf18a & 0xFF})")

        if args.cckpd is not None:
            t.write8(0x0A0A, int(args.cckpd, 0))
            print(f"  [override] 0xA0A <- 0x{int(args.cckpd, 0):02x}")
        if args.igi is not None:
            g = int(args.igi, 0) & 0x7F
            for reg in (0x0C50, 0x0E50):
                t.write32(reg, (t.read32(reg) & ~0x7F) | g)
            # Mirror odm_write_dig: CCK new-AGC IGI 0xA0C[13:8] = igi>>1, so CCK gain follows too.
            t.write32(0x0A0C, (t.read32(0x0A0C) & ~0x3F00) | (((g >> 1) & 0x3F) << 8))
            print(f"  [override] IGI 0xC50/0xE50<-0x{g:02x}, CCK 0xA0C[13:8]<-0x{(g >> 1) & 0x3F:02x}")
        for spec in args.set:
            reg, _, rest = spec.partition("=")
            val, _, szs = rest.partition(":")
            reg, val = int(reg, 0), int(val, 0)
            sz = int(szs) if szs else 4
            {1: t.write8, 2: t.write16, 4: t.write32}[sz](reg, val)
            print(f"  [override] 0x{reg:X} <- 0x{val:X} ({sz}B)")

        if args.igisweep:
            vals = [int(v, 0) & 0x7F for v in args.igisweep.split(",")]
            seg = args.dwell / len(vals)
            target = (args.bssid or "").lower()
            print(f"\n  IGI sweep, ch{args.channel}, {seg:.0f}s/step, "
                  f"focus={target or 'top CCK AP/step'}  (goal: 8-10 bcn/s, 0 zero-secs):")
            print(f"    {'IGI':>5} {'0xA0C':>5} {'bcn':>4} {'bcn/s':>6} {'zero-s':>6} {'rssi':>5}  AP")
            for g in vals:
                for reg in (0x0C50, 0x0E50):
                    t.write32(reg, (t.read32(reg) & ~0x7F) | g)
                t.write32(0x0A0C, (t.read32(0x0A0C) & ~0x3F00) | (((g >> 1) & 0x3F) << 8))
                seg_tally = Tally()
                s0 = time.monotonic()
                while time.monotonic() - s0 < seg:
                    buf = t.bulk_in()
                    if buf:
                        seg_tally.walk(buf, time.monotonic())
                foc = target
                if not foc:
                    rows = sorted(((sum(n for r, n in pr.items() if _is_cck(r)), b)
                                   for b, pr in seg_tally.beacons.items()), reverse=True)
                    foc = rows[0][1] if rows else "-"
                nsec = max(1, int(seg))
                tot = sum(seg_tally.buckets.get(s, {}).get(foc, 0) for s in range(nsec))
                zeros = sum(1 for s in range(nsec)
                            if seg_tally.buckets.get(s, {}).get(foc, 0) == 0)
                print(f"    0x{g:02x}  0x{(g >> 1) & 0x3F:02x}  {tot:>4} {tot / seg:>6.1f} "
                      f"{zeros:>6} {seg_tally.rssi.get(foc, '?'):>5}  {foc}")
            return 0

        wd = None
        if args.watchdog:
            wd = dm_watchdog.DigState(cur_ig_value=t.read32(0x0C50) & 0x7F,
                                      cck_new_agc=bool((t.read32(0x0A9C) >> 17) & 1))
        tally = Tally()
        start = last_wd = time.monotonic()
        while True:
            now = time.monotonic()
            if now - start >= args.dwell:
                break
            if wd is not None and now - last_wd >= 2.0:
                dm_watchdog.phydm_watchdog(t, wd)
                last_wd = now
            buf = t.bulk_in()
            if buf:
                tally.walk(buf, now)
        if wd is not None:
            print(f"  [watchdog] converged IGI 0xC50=0x{t.read32(0x0C50) & 0x7F:02x}  "
                  f"0xA0A=0x{t.read8(0x0A0A):02x}  (DigState cur_ig=0x{wd.cur_ig_value:02x}, "
                  f"cck_pd_lv={wd.cck_pd_lv})")
        tally.report(args.channel, args.dwell)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    sys.exit(main())
