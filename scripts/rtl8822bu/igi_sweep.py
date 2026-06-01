"""RTL8822BU IGI sweep — does adaptive RX gain (DIG) actually matter here?

The DIG watchdog walks the OFDM initial gain index (IGI) to keep the receiver
in its linear range. To know whether that helps *in this RF environment* — and
whether DIG's coverage band lands on the optimum — we measure the chip's own
decode rate as a function of a *pinned* IGI.

The metric is the BB CRC-ok counters (rtw8822b_false_alarm_statistics,
rtw8822b.c:1041-1060): the number of frames the PHY demodulated with a good CRC
per window, summed across CCK/OFDM/HT/VHT. Unlike host-side beacon counting it
is immune to USB/UI/parse starvation and counts *every* good frame on air, so it
is the honest "how well is the receiver hearing" number. We also log CRC-err
(marginal frames), false-alarm (noise), and CCA (channel busy) per window.

Procedure: bring the card up on a fixed channel, then for each IGI in the range
pin it on both OFDM paths (0xc50/0xe50), settle, reset the counters, dwell, and
read. Several passes are averaged so bursty traffic doesn't bias one IGI. The
DIG watchdog is never started here, so nothing fights the pinned value.

How to read the result:
  - **Flat curve** -> IGI does not matter in this environment (AGC default is
    already fine); DIG is benign insurance and "feels the same" is correct.
  - **Peak inside [0x1c, 0x2a]** -> DIG's coverage band can find the optimum.
  - **Peak below 0x1c** -> the receiver wants more sensitivity than DIG's floor
    allows; the monitor coverage floor may be leaving frames on the table.
  - **CRC-ok collapses / FA explodes at low IGI** -> saturation/noise regime the
    watchdog is meant to back away from.

Pick a channel with a *stable* signal source for a clean read (e.g. a nearby AP
you can see steadily; the dd-wrt WEP box on ch 6 works well).

Usage:
    .venv/Scripts/python.exe scripts/rtl8822bu/igi_sweep.py --channel 6
    .venv/Scripts/python.exe scripts/rtl8822bu/igi_sweep.py --channel 36 \
        --lo 0x10 --hi 0x32 --step 2 --dwell 4 --passes 3
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

from wifit3.chips.rtl8822bu.chan import (
    set_channel_2g_20mhz,
    set_channel_5g_20mhz,
)
from wifit3.chips.rtl8822bu.constants import REG_SYS_CFG1, USB_IDS_8822BU
from wifit3.chips.rtl8822bu.dynamic import (
    BIT_CCK_EN,
    DIG_CVRG_MAX,
    DIG_CVRG_MIN,
    DIG_IGI_MASK,
    REG_CCK_DEMOD,
    REG_DIG_PATH,
    REG_FA_CCK,
    REG_FA_OFDM,
)
from wifit3.chips.rtl8822bu.firmware import (
    download_firmware,
    download_firmware_validate,
    load_firmware_blob,
)
from wifit3.chips.rtl8822bu.mac import (
    cut_mask_from_sys_cfg1,
    is_chip_warm,
    mac_init_for_rx,
    mac_power_on,
)
from wifit3.chips.rtl8822bu.phy import EfuseDefaults, phy_set_param
from wifit3.chips.rtl8822bu.transport import RTL8822BUTransport

# BB statistics counters (rtw8822b_false_alarm_statistics, rtw8822b.c:1041-1060).
# Each crc32 reg packs ok in the low 16 bits, err in the high 16.
REG_CRC_CCK = 0x0F04
REG_CRC_OFDM = 0x0F14
REG_CRC_HT = 0x0F10
REG_CRC_VHT = 0x0F0C
REG_CCA_OFDM = 0x0F08   # ofdm cca in high 16
REG_CCA_CCK = 0x0FCC    # cck  cca in low 16


def open_device():
    backend = libusb_package.get_libusb1_backend()
    for vid, pid, desc in USB_IDS_8822BU:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is not None:
            print(f"  Found {vid:04x}:{pid:04x}  {desc}")
            break
    else:
        print("No RTL8822BU found. Plug it in, confirm Zadig/WinUSB, retry.")
        sys.exit(1)
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    return dev


def bring_up(dev, transport: RTL8822BUTransport, channel: int) -> None:
    """Power-on (cold) + PHY/MAC init + channel tune — enough for RX. No DIG
    watchdog is started, so the sweep fully owns IGI."""
    if is_chip_warm(transport):
        print("  Chip WARM — skipping FW upload (prior session still running).")
    else:
        print("  Chip COLD — power-on + FW upload.")
        cut_mask = cut_mask_from_sys_cfg1(transport.read32(REG_SYS_CFG1))
        mac_power_on(transport, cut_mask=cut_mask)
        download_firmware(dev, transport, load_firmware_blob())
        ok_run, last = download_firmware_validate(transport)
        if not ok_run:
            print(f"  FW_READY not satisfied (MCUFW_CTRL=0x{last:08x}) — abort.")
            sys.exit(1)
        phy_set_param(transport, EfuseDefaults())
        mac_init_for_rx(transport)
    tune = set_channel_2g_20mhz if channel <= 14 else set_channel_5g_20mhz
    tune(transport, channel)
    print(f"  Tuned to channel {channel}.")


def read_igi(transport: RTL8822BUTransport) -> int:
    return transport.read32(REG_DIG_PATH[0]) & DIG_IGI_MASK


def pin_igi(transport: RTL8822BUTransport, igi: int) -> None:
    for addr in REG_DIG_PATH:
        transport.write32_mask(addr, DIG_IGI_MASK, igi & DIG_IGI_MASK)


def read_and_reset_stats(transport: RTL8822BUTransport) -> dict:
    """Snapshot the BB CRC-ok/err + FA + CCA counters, then reset them for the
    next window (full port of rtw8822b_false_alarm_statistics)."""
    cck_en = bool(transport.read32(REG_CCK_DEMOD) & BIT_CCK_EN)

    def ok_err(addr):
        v = transport.read32(addr)
        return v & 0xFFFF, (v >> 16) & 0xFFFF

    cck_ok, cck_err = ok_err(REG_CRC_CCK)
    ofdm_ok, ofdm_err = ok_err(REG_CRC_OFDM)
    ht_ok, ht_err = ok_err(REG_CRC_HT)
    vht_ok, vht_err = ok_err(REG_CRC_VHT)
    fa_cck = transport.read16(REG_FA_CCK)
    fa_ofdm = transport.read16(REG_FA_OFDM)
    ofdm_cca = (transport.read32(REG_CCA_OFDM) >> 16) & 0xFFFF
    cck_cca = (transport.read32(REG_CCA_CCK) & 0xFFFF) if cck_en else 0

    # Reset (rtw8822b.c:1063-1068) — per-register set/clr order matters.
    transport.write32_set(0x9A4, 1 << 17)
    transport.write32_clr(0x9A4, 1 << 17)
    transport.write32_clr(0xA2C, 1 << 15)
    transport.write32_set(0xA2C, 1 << 15)
    transport.write32_set(0xB58, 1 << 0)
    transport.write32_clr(0xB58, 1 << 0)

    return dict(
        ok=cck_ok + ofdm_ok + ht_ok + vht_ok,
        err=cck_err + ofdm_err + ht_err + vht_err,
        fa=fa_ofdm + (fa_cck if cck_en else 0),
        cca=ofdm_cca + cck_cca,
        cck_ok=cck_ok, ofdm_ok=ofdm_ok, ht_ok=ht_ok, vht_ok=vht_ok,
    )


def measure(transport, igi: int, dwell: float, settle: float) -> dict:
    pin_igi(transport, igi)
    time.sleep(settle)              # let AGC settle at the new gain
    read_and_reset_stats(transport)  # discard the settle window
    time.sleep(dwell)
    s = read_and_reset_stats(transport)
    return {k: v / dwell for k, v in s.items()}  # per-second


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", type=int, default=1,
                   help="Channel to dwell on (pick one with a stable AP).")
    p.add_argument("--lo", type=lambda x: int(x, 0), default=0x10,
                   help="Lowest IGI to test (default 0x10).")
    p.add_argument("--hi", type=lambda x: int(x, 0), default=0x32,
                   help="Highest IGI to test, inclusive (default 0x32).")
    p.add_argument("--step", type=int, default=2, help="IGI step (default 2).")
    p.add_argument("--dwell", type=float, default=4.0,
                   help="Measurement seconds per IGI per pass (default 4).")
    p.add_argument("--settle", type=float, default=0.5,
                   help="Settle seconds after writing IGI (default 0.5).")
    p.add_argument("--passes", type=int, default=3,
                   help="Sweep passes to average (default 3).")
    p.add_argument("--fa-ceiling", type=float, default=2000.0,
                   help="false-alarm/sec ceiling above which an IGI is treated "
                        "as noise-dominated, not a usable operating point "
                        "(default 2000).")
    args = p.parse_args()

    print("--- USB discovery + bring-up ---")
    dev = open_device()
    transport = RTL8822BUTransport(dev)
    try:
        bring_up(dev, transport, args.channel)
        agc_default = read_igi(transport)
        print(f"  AGC-table default IGI = 0x{agc_default:02x}")
        print(f"  DIG coverage band     = [0x{DIG_CVRG_MIN:02x}, "
              f"0x{DIG_CVRG_MAX:02x}]\n")

        igis = list(range(args.lo, args.hi + 1, args.step))
        # samples[igi] = list of per-second stat dicts across passes
        samples: dict[int, list[dict]] = {igi: [] for igi in igis}
        for pass_n in range(args.passes):
            print(f"--- pass {pass_n + 1}/{args.passes} ---", file=sys.stderr)
            for igi in igis:
                r = measure(transport, igi, args.dwell, args.settle)
                samples[igi].append(r)
                print(f"  IGI 0x{igi:02x}: ok/s={r['ok']:7.1f} "
                      f"err/s={r['err']:6.1f} fa/s={r['fa']:7.1f} "
                      f"cca/s={r['cca']:7.1f}", file=sys.stderr)

        # Average across passes.
        def avg(igi, key):
            xs = [s[key] for s in samples[igi]]
            return sum(xs) / len(xs) if xs else 0.0

        rows = [(igi, avg(igi, "ok"), avg(igi, "err"), avg(igi, "fa"),
                 avg(igi, "cca")) for igi in igis]
        raw_peak = max(rows, key=lambda r: r[1])

        print("\n=== decode rate vs IGI "
              f"(channel {args.channel}, {args.passes}x{args.dwell:.0f}s) ===")
        print(f"{'IGI':>5} {'ok/s':>9} {'err/s':>8} {'fa/s':>9} "
              f"{'cca/s':>9}   notes")
        for igi, ok, err, fa, cca in rows:
            notes = []
            if igi == raw_peak[0]:
                notes.append("<-- raw decode peak")
            if igi == agc_default:
                notes.append("AGC default")
            if igi in (DIG_CVRG_MIN, DIG_CVRG_MAX):
                notes.append("DIG band edge")
            inband = "" if DIG_CVRG_MIN <= igi <= DIG_CVRG_MAX else " (out of DIG band)"
            print(f"0x{igi:02x} {ok:9.1f} {err:8.1f} {fa:9.1f} {cca:9.1f}   "
                  f"{', '.join(notes)}{inband}")

        # Verdict — the receiver's goal is max decode WITHOUT drowning in false
        # alarms, so raw ok/s alone is a trap: at very high gain (low IGI) the
        # front-end triggers on noise and `fa/s` explodes while real decode
        # barely moves. The honest optimum is the best decode among IGIs whose
        # false-alarm rate stays under --fa-ceiling.
        print("\n=== verdict ===")
        usable = [r for r in rows if r[3] < args.fa_ceiling]
        storm = "" if raw_peak[3] < args.fa_ceiling else \
            f" — NOISE STORM ({raw_peak[3]:.0f} fa/s), not a real optimum"
        print(f"  raw decode peak: 0x{raw_peak[0]:02x} @ {raw_peak[1]:.1f} ok/s{storm}")

        if not usable:
            print(f"  No IGI held false alarms under {args.fa_ceiling:.0f}/s — the "
                  "whole channel is noise-dominated. Try a quieter channel.")
        else:
            rec = max(usable, key=lambda r: r[1])
            cliff = min(r[0] for r in usable)
            ok_usable = [r[1] for r in usable]
            spread = max(ok_usable) - min(ok_usable)
            rel = spread / max(ok_usable) if max(ok_usable) else 0.0
            print(f"  recommended (max decode, fa/s < {args.fa_ceiling:.0f}): "
                  f"0x{rec[0]:02x} @ {rec[1]:.1f} ok/s, fa/s={rec[3]:.0f}")
            print(f"  false-alarm cliff: fa/s stays controlled only at IGI >= "
                  f"0x{cliff:02x}; below that the receiver drowns in noise.")
            agc_ok = {r[0]: r[1] for r in rows}.get(agc_default)
            if rec[0] < DIG_CVRG_MIN:
                print(f"  -> optimum 0x{rec[0]:02x} is below the DIG floor "
                      f"0x{DIG_CVRG_MIN:02x} AND false-alarm-controlled — the "
                      "coverage floor may be costing yield here.")
            elif rec[0] > DIG_CVRG_MAX:
                print(f"  -> optimum 0x{rec[0]:02x} is above the DIG ceiling "
                      f"0x{DIG_CVRG_MAX:02x}.")
            else:
                tail = ""
                if agc_ok is not None and rec[1]:
                    tail = (f" AGC default 0x{agc_default:02x} = {agc_ok:.1f} ok/s "
                            f"({agc_ok / rec[1] * 100:.0f}% of optimum).")
                print(f"  -> optimum 0x{rec[0]:02x} is INSIDE the DIG band "
                      f"[0x{DIG_CVRG_MIN:02x},0x{DIG_CVRG_MAX:02x}] and "
                      f"false-alarm-controlled — DIG seeds/holds here correctly.{tail}")
            if rel < 0.10:
                print("  Decode is ~flat across the usable range, so steady-state "
                      "yield is gain-insensitive here; DIG's value is keeping IGI "
                      "OUT of the noise-storm region, not boosting throughput.")
    finally:
        print("\n--- release ---")
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError as e:
            print(f"  (release warning: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
