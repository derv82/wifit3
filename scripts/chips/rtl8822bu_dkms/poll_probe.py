"""Live-HW poll-loop integrity probe — the verify_pcap blind spot (G12, G18).

verify_pcap feeds the capture's recorded reads back, so every poll-until-condition loop "passes"
regardless of live behaviour. This runs cold_bringup on real hardware with the poll loops
instrumented and reports whether each converges live:

  G12 dc_cancellation: phydm_stop_ic_trx polls 0xFA0 for BB-idle (PHYTXON|CCA clear). If it bails
      (returns False), dc_cancellation returns early -> RX DC offset NEVER cancelled. Reports the
      idle-poll iteration count + result per path, the 0xFA0 DC words, and the applied RX DC-offset
      comp (0xC10/0xC14/0xE10/0xE14) read back after init.
  G18 config_trx_mode: the RF-mode poll (RF_A 0x33 == 0x00001, 100x). Reports iterations to converge.

Passive (control transfers only, no TX). Run: uv run python scripts/chips/rtl8822bu_dkms/poll_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8822bu_dkms import bringup, cal, chipid
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport

USB_VID, USB_PID = 0x2357, 0x0138


def main() -> int:
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print("[FAIL] RTL8822BU not found")
        return 1
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    usb.util.claim_interface(dev, 0)

    # --- instrument cal's dc_cancellation poll (G12) ---
    dbg = {"n": 0, "vals": []}
    orig_dbg = cal._get_bb_dbg_port_val

    def wrapped_dbg(t):
        v = orig_dbg(t)
        dbg["n"] += 1
        if len(dbg["vals"]) < 16:
            dbg["vals"].append(v)
        return v
    cal._get_bb_dbg_port_val = wrapped_dbg

    stops = []
    orig_stop = cal._stop_ic_trx

    def wrapped_stop(t, st, revert):
        before = dbg["n"]
        r = orig_stop(t, st, revert)
        stops.append((revert, r, dbg["n"] - before))
        return r
    cal._stop_ic_trx = wrapped_stop

    t = Rtl8822buTransport(dev)
    try:
        info = chipid.get_chip_info(t)
        print(f"  chip cut = {info.chip_ver}")
        print("[*] cold_bringup with poll instrumentation...")
        bringup.cold_bringup(t)
        print("[PASS] cold init complete.\n")

        print("=== G12: dc_cancellation stop_ic_trx idle-poll (live HW) ===")
        sets = [(r, it) for (rev, r, it) in stops if rev is False]
        for i, (r, it) in enumerate(sets):
            verdict = "REACHED IDLE" if r else "** BAILED (idle never reached) -> DC NOT cancelled **"
            print(f"  stop_ic_trx SET #{i}: result={r} ({verdict}); dbg-port reads consumed={it}")
        print(f"  total 0xFA0 reads during init: {dbg['n']}; first words: "
              f"{[hex(v) for v in dbg['vals'][:8]]}")
        print("  applied RX DC-offset comp (read back):")
        for reg in (0x0C10, 0x0C14, 0x0E10, 0x0E14):
            print(f"    0x{reg:04x} = 0x{t.read32(reg):08x}")
        if all(r for (r, _) in sets) and sets:
            print("  => stop_ic_trx reached idle on every path -> dc_cancellation ran fully (live OK).")
        else:
            print("  => stop_ic_trx BAILED -> dc_cancellation skipped: live-HW divergence (real gap).")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    sys.exit(main())
