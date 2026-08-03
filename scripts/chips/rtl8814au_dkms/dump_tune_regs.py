"""Live tune-register read-back — is the 8814au ACTUALLY on 20 MHz / ch1?

`verify_pcap` proves our tune *writes* match the cold-boot capture, but it feeds the
port the recorded reads — so it cannot catch a read-modify-write that lands on a wrong
value on live silicon (the bandwidth / center-frequency registers are all RMWs). This
script brings the card up exactly as the driver does, then READS BACK the actual
bandwidth/channel registers off the chip and checks them against the 20 MHz/ch1 values
the tune intends. A divergence here = we are mis-tuned (e.g. skewed toward 40/80 MHz),
which would make us a wide, mis-centered receiver: hears lots of adjacent data traffic,
goes deaf on the one 20 MHz reference AP.

    uv run python scripts/chips/rtl8814au_dkms/dump_tune_regs.py [--channel 1]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8814au_dkms import constants as C
from wifit3.chips.rtl8814au_dkms.bb import phy_bb_config
from wifit3.chips.rtl8814au_dkms.chan import init_tune, set_channel_bw, set_rfe_reg_init
from wifit3.chips.rtl8814au_dkms.dm import init_hal_dm
from wifit3.chips.rtl8814au_dkms.efuse import read_chip_params
from wifit3.chips.rtl8814au_dkms.firmware import bring_up
from wifit3.chips.rtl8814au_dkms.mac import hal_init_turn_on, mac_init_misc, phy_mac_config
from wifit3.chips.rtl8814au_dkms.monitor import enable_rx_bar, enter_monitor, set_sta_opmode
from wifit3.chips.rtl8814au_dkms.rf import _rf_read, phy_rf_config
from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport

FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"


def _find_dev():
    dev = usb.core.find(idVendor=C.VID_REALTEK, idProduct=C.PID_RTL8814AU,
                        backend=libusb_package.get_libusb1_backend())
    if dev is None:
        print("[-] RTL8814AU (0bda:8813) not found.", file=sys.stderr)
        raise SystemExit(1)
    return dev


def _bring_up(t, channel: int):
    fw = FW_BIN.read_bytes()
    p = read_chip_params(t)
    if not bring_up(t, fw):
        raise SystemExit("[-] firmware not ready")
    phy_mac_config(t)
    mac_init_misc(t)
    phy_bb_config(t, p.rfe_type, p.crystal_cap)
    phy_rf_config(t, p.rfe_type)
    init_tune(t, channel, p.tx_power, p.tx_power_5g, p.bb_swing, p.bb_swing_5g)
    init_hal_dm(t)
    set_rfe_reg_init(t, p.rfe_type)
    hal_init_turn_on(t, p.mac_address)
    enable_rx_bar(t)
    set_channel_bw(t, channel, p.tx_power, p.tx_power_5g, p.bb_swing, p.bb_swing_5g,
                   current_band=C.BAND_MAX)
    set_sta_opmode(t, p.mac_address)
    enter_monitor(t)


def _chk(label, value, ok, expect):
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label:<34} = 0x{value:08x}   expect {expect}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=1)
    args = ap.parse_args()
    ch = args.channel

    t = Rtl8814auTransport(_find_dev())
    print(f"[*] bringing up on ch{ch}...", file=sys.stderr)
    _bring_up(t, ch)
    print(f"\n[*] LIVE tune-register read-back (expecting ch{ch} @ 20 MHz):")

    all_ok = True
    # --- RF 0x18 per path: channel [9:0]+[18:16] (mask 0x703ff), BW [11:10] = 3 (20 MHz) ---
    for path in ("a", "b", "c", "d"):
        v = _rf_read(t, path, C.RF_CHNLBW)
        chan_bits = v & C.RF_CHNLBW_CH_MASK
        bw = (v & C.RF_CHNLBW_BW_MASK) >> 10
        ok = (chan_bits & 0xFF) == ch and bw == 3
        all_ok &= _chk(f"RF path-{path} 0x18 (ch={chan_bits & 0xFF} bw={bw})", v, ok,
                       f"ch={ch} bw=3(20MHz)")

    # --- BB bandwidth/center-frequency registers ---
    v = t.read32(C.rRFMOD)
    all_ok &= _chk("BB 0x8ac rRFMOD [1:0] (ADC bw)", v, (v & 0x3) == 0, "[1:0]=0 (20MHz)")

    v = t.read32(C.rAGC_table_Jaguar)
    all_ok &= _chk("BB 0x82c rAGC [15:12] (AGC bw)", v, ((v >> 12) & 0xF) == 6, "[15:12]=6 (20MHz)")

    v = t.read32(C.rFc_area)
    all_ok &= _chk("BB 0x860 rFc_area [28:17]", v, ((v >> 17) & 0xFFF) == 0x96A,
                   "[28:17]=0x96A (2.4G)")

    v = t.read16(C.REG_TRXPTCL_CTL)
    all_ok &= _chk("MAC 0x668 TRXPTCL_CTL [8:7]", v, (v & 0x180) == 0, "BIT7|BIT8 clear (20MHz)")

    v = t.read8(C.REG_DATA_SC)
    all_ok &= _chk("MAC 0x483 DATA_SC (2ndary ch)", v, v == 0, "0 (no 2ndary)")

    v = t.read8(C.REG_CCK_CHECK)
    all_ok &= _chk("MAC 0x454 CCK_CHECK bit7 (band)", v, (v & 0x80) == 0, "bit7 clear (2.4G)")

    # --- CCK RX path / antenna / packet-detect (the ref AP beacons 100% CCK) ---
    print("  --- CCK RX path/antenna/PD ---")
    v = t.read32(0x0A04)   # rCCK_RX: [31:28]=pathB tx=4, [27:24]=pathB rx=5
    all_ok &= _chk("BB 0x0a04 CCK_RX (txB/rxB)", v,
                   ((v >> 28) & 0xF) == 0x4 and ((v >> 24) & 0xF) == 0x5, "[31:28]=4 [27:24]=5")
    v = t.read32(0x0A00)
    all_ok &= _chk("BB 0x0a00 CCK ant-div [15]", v, (v & (1 << 15)) == 0, "[15]=0 (ant-div off)")
    v = t.read32(0x0A70)
    all_ok &= _chk("BB 0x0a70 concurrent-CCA [7]", v, (v & (1 << 7)) == 0, "[7]=0")
    v = t.read32(0x0A74)
    all_ok &= _chk("BB 0x0a74 RX path-div [8]", v, (v & (1 << 8)) == 0, "[8]=0")
    v = t.read32(0x0A14)
    all_ok &= _chk("BB 0x0a14 mrc_antsel [7]", v, (v & (1 << 7)) == 0, "[7]=0")
    v = t.read32(0x0A20)
    all_ok &= _chk("BB 0x0a20 MBC weight [5:4]", v, ((v >> 4) & 0x3) == 1, "[5:4]=1")
    v = t.read32(0x0A84)
    all_ok &= _chk("BB 0x0a84 2R-CCA-only [28]", v, (v & (1 << 28)) != 0, "[28]=1")
    v = t.read8(0x0A0A)
    all_ok &= _chk("BB 0x0a0a CCK-PD threshold", v, v == 0x40, "0x40 (cck_pd_init level0)")

    t.close()
    if all_ok:
        print(f"\nPASS — chip is on 20 MHz / ch{ch} as intended.")
    else:
        print(f"\nFAIL — chip is NOT cleanly on 20 MHz / ch{ch} (see FAILs above): mis-tuned.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
