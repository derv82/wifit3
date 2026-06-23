"""RTL8821CU (8821cu_dkms) — RX-path diagnostic for the "init OK but no 802.11 frames" case.

The offline gate verifies every host->chip write byte-for-byte, but cannot see the chip->host
direction or how live read-backs feed the RF/tune math. This probe runs the real cold bring-up,
then on live silicon:

  snapshot : read back the monitor RX config (RCR / RXFLTMAP / MSR), the tune (RF 0x18 channel,
             central freq, AGC idx, IGI), and the antenna routing (DPDT) — to confirm the
             registers actually hold the monitor/WiFi state on this card.
  fa-dwell : sample the raw page-F counters over a few seconds — a coarse "is the BB counting"
             probe (the masked false-alarm count the DIG uses is a subset of these raw reads).
  rx-tally : a short bulk-IN loop splitting C2H reports from 802.11 MPDUs from empty reads.

Passive: no 802.11 TX. Usage (card WinUSB-bound, Wi-Fi mode 0bda:c820):
    uv run python scripts/rtl8821cu_dkms/rx_diag.py --dwell 6
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8821cu_dkms import bringup, btc, chipid, efuse, watchdog
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

USB_VID, USB_PID = 0x0BDA, 0xC820
FW_BULK_OUT_EP = 0x05
_WIFI_INTF_CLASS = 0xFF

# Monitor RX config
_REG_RCR = 0x0608               # promiscuous monitor RCR should read 0x90000001 (APP_FCS|accept-all)
_REG_MSR = 0x0102               # opmode: monitor => NOLINK
_REG_RXFLTMAP = (0x06A0, 0x06A2, 0x06A4)        # mgmt / ctrl / data accept maps (monitor: 0xffff)
# Tune
_REG_CENTRAL_FREQ = 0x0860      # [28:17] clock-offset central freq (2.4G ch1 => 0x96a)
_REG_AGC_IDX = 0x0C1C           # [11:8] AGC table idx
_REG_IGI = 0x0C50               # [6:0] initial gain
# Antenna / RF
_REG_DPDT = 0x0CB4              # DPDT ant switch routing
_REG_LED_ANT = 0x004E
# PHY false-alarm / CCA counters (free-running; we read deltas)
_REG_OFDM_FA = 0x0F48
_REG_CCK_FA = 0x0A5C
_REG_CCA = (0x0F08, 0x0F0C, 0x0F10, 0x0F50, 0x0F54)


def _open():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] Wi-Fi function {USB_VID:04x}:{USB_PID:04x} not found (ZeroCD? Zadig?).")
        return None, None
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
    if intf is None:
        print("[FAIL] no vendor (WiFi) interface")
        return None, None
    usb.util.claim_interface(dev, intf)
    return dev, intf


def _snapshot(t) -> None:
    rcr = t.read32(_REG_RCR)
    msr = t.read8(_REG_MSR)
    flt = [t.read16(r) for r in _REG_RXFLTMAP]
    rf18 = read_rf(t, 0x18)
    rf00 = read_rf(t, 0x00)
    fc = (t.read32(_REG_CENTRAL_FREQ) >> 17) & 0xFFF
    agc = (t.read32(_REG_AGC_IDX) >> 8) & 0xF
    igi = t.read32(_REG_IGI) & 0x7F
    dpdt = t.read32(_REG_DPDT)
    led = t.read8(_REG_LED_ANT)
    cr = t.read16(0x0100)
    rxdma = t.read8(0x0286)
    r808 = t.read32(0x0808)
    ra04 = t.read32(0x0A04)
    print("\n[snapshot] monitor RX config + tune + antenna")
    print(f"  0x808      0x{r808:08x}   CCK_EN[28]={r808 >> 28 & 1}  (0 => CCK receiver off)  "
          f"0xa04=0x{ra04:08x} (cck_tx_path[31:28]={ra04 >> 28 & 0xF})")
    cr_bits = (f"HCI_RXDMA={cr >> 1 & 1} RXDMA_EN={cr >> 3 & 1} PROT={cr >> 4 & 1} "
               f"SCHED={cr >> 5 & 1} MACTX={cr >> 6 & 1} MACRX={cr >> 7 & 1}")
    print(f"  REG_CR     0x{cr:04x}       {cr_bits}  (RXDMA_EN+MACRX must be 1 for RX)")
    print(f"  0x286      0x{rxdma:02x}         RXDMA mode/agg byte")
    print(f"  RCR        0x{rcr:08x}   (expect 0x90000001 promiscuous monitor)")
    print(f"  MSR        0x{msr:02x}         RXFLTMAP mgmt/ctrl/data = "
          f"0x{flt[0]:04x}/0x{flt[1]:04x}/0x{flt[2]:04x} (expect 0xffff each)")
    print(f"  RF 0x18    0x{rf18:05x}      channel[7:0]=0x{rf18 & 0xFF:02x} (expect 0x01)")
    print(f"  RF 0x00    0x{rf00:05x}      (RF mode/standby — 0 => RF off)")
    print(f"  central_fc 0x{fc:03x}        AGC_idx={agc}  IGI=0x{igi:02x}")
    print(f"  DPDT 0xCB4 0x{dpdt:08x}   0x4E=0x{led:02x}  (antenna routing)")


def _counter_total(t, regs) -> int:
    return sum(t.read32(r) for r in regs)


def _fa_dwell(t, dwell: float) -> None:
    print(f"\n[fa-dwell] sampling PHY false-alarm/CCA counters for {dwell:g}s")
    o0, c0, a0 = t.read32(_REG_OFDM_FA), t.read32(_REG_CCK_FA), _counter_total(t, _REG_CCA)
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        o, c, a = t.read32(_REG_OFDM_FA), t.read32(_REG_CCK_FA), _counter_total(t, _REG_CCA)
        print(f"  d(ofdm_fa)={(o - o0) & 0xFFFFFFFF:>8}  d(cck_fa)={(c - c0) & 0xFFFFFFFF:>8}  "
              f"d(cca_sum)={(a - a0) & 0xFFFFFFFF:>10}")
        o0, c0, a0 = o, c, a
    print("  ^ raw 32-bit reads (the masked FA the watchdog/DIG actually uses is a subset of these);")
    print("    treat as a coarse 'is the BB counting' probe, not an FA count.")


def _rx_tally(t, dwell: float) -> None:
    print(f"\n[rx-tally] bulk-IN for {dwell:g}s")
    bufs = nbytes = pkts = c2h = good = empty = 0
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        buf = t.bulk_in()
        if not buf:
            empty += 1
            continue
        bufs += 1
        nbytes += len(buf)
        off, n = 0, len(buf)
        while off + 24 <= n:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            pkt_off = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + pkt_off > n:
                break
            pkts += 1
            c2h += 1 if d.rpt_sel else 0
            good += 1 if not (d.rpt_sel or d.crc_err or d.icv_err) else 0
            off += (pkt_off + 7) & ~7
    print(f"  bufs={bufs} bytes={nbytes} empty_reads={empty}  pkts={pkts} c2h={c2h} good_80211={good}")


def _watchdog_experiment(t, info, ticks: int, dwell: float) -> None:
    """Run the (already byte-verified) phydm dynamic-check tick a few times — the runtime DIG the
    kernel runs every ~2s. With the false-alarm flood, DIG should raise IGI (0xc50) toward its
    no-link max; then re-tally RX to see whether suppressing false alarms unblocks real frames."""
    st = watchdog.WatchdogState(eeprom_thermal=info.eeprom_thermal,
                                thermal_offset=efuse.thermal_offset(info))
    print(f"\n[watchdog] running {ticks} phydm ticks (DIG vs the false-alarm flood)")
    for i in range(ticks):
        watchdog.tick(t, st)
        igi = t.read32(_REG_IGI) & 0x7F
        print(f"  tick {i + 1:2}: IGI=0x{igi:02x}  cck_pd_lv={st.cck_pd_lv}")
        time.sleep(0.3)
    print("  re-tallying RX after DIG adaptation:")
    _rx_tally(t, dwell)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwell", type=float, default=6.0)
    ap.add_argument("--ticks", type=int, default=12)
    args = ap.parse_args()

    dev, intf = _open()
    if dev is None:
        return 1
    print(f"[*] claimed WiFi interface {intf}")
    t = Rtl8821cuTransport(dev, bulk_out_ep=FW_BULK_OUT_EP)
    try:
        cid, cver = chipid.mount_get_chip_info(t)
        print(f"[*] chip_id=0x{cid:x} cut={cver}; running cold bring-up...")
        info = bringup.cold_bringup(t)
        print(f"[*] cold init OK. EFUSE: mac={getattr(info, 'mac_address', '?')} "
              f"rfe=0x{getattr(info, 'rfe_type', 0):x} xtal=0x{getattr(info, 'crystal_cap', 0):x} "
              f"thermal={getattr(info, 'eeprom_thermal', 0)}")
        _snapshot(t)
        _rx_tally(t, args.dwell)
        # A/B the RCR: the airmon capture's 0x90000001 is AAP|APP_PHYSTS|APP_FCS only — no
        # AB (broadcast) / AMF (mgmt) accept bits, so the MAC drops beacons. The sibling Jaguar
        # drivers use 0x9000382f (adds APM|AM|AB|ADF|ACF|AMF). Rewrite and re-tally.
        print("\n[rcr A/B] rewriting RCR 0x90000001 -> 0x9000382f (adds AB|AM|ADF|ACF|AMF)")
        t.write32(_REG_RCR, 0x9000382F)
        print(f"  RCR now 0x{t.read32(_REG_RCR):08x}")
        _rx_tally(t, args.dwell)
        # Probe: force the PHASE_WONLY antenna grant (coex owner WL + GNT_BT low + GNT_WL high),
        # read 0x38 back to confirm it lands, and re-tally.
        print("\n[gnt force-wifi] coex_ctrl_owner(WL) + GNT_BT=SW_LOW + GNT_WL=SW_HIGH")
        before = btc._read_indirect(t, 0x38)
        btc._write_bitmask8(t, btc.REG_COEX_CTRL_OWNER, 1 << 2, 1)
        btc._set_gnt_bt(t, btc._GNT_SW_LOW)
        btc._set_gnt_wl(t, btc._GNT_SW_HIGH)
        after = btc._read_indirect(t, 0x38)
        print(f"  ltecoex 0x38: 0x{before:08x} -> 0x{after:08x}  (GNT_WL nibbles -> SW_HIGH=0x3)")
        print(f"  path owner 0x73=0x{t.read8(0x73):02x} (bit2=1 => WLSIDE)  DPDT 0xcb4=0x{t.read32(0x0cb4):08x}")
        _rx_tally(t, args.dwell)
        return 0
    finally:
        try:
            usb.util.release_interface(dev, intf)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    sys.exit(main())
