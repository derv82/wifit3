"""Acceptance gate: replay-diff the EFUSE read against the cold-boot capture.

The probe-phase efuse read (device 51, frames 51..5677) precedes _InitPowerOn, so
it sits outside the M1+ window that ``verify_pcap.py`` covers. This verifier
replays just that window: it drives ``efuse.read_chip_params`` against a transport
that feeds back the chip's recorded reads, asserts the port emits byte-identical
EFUSE_CTRL traffic, and cross-checks the decoded params against the values the BB
config independently confirmed (rfe_type=1, crystal_cap=0x23).

Run: ``uv run python scripts/rtl8814au_dkms/verify_efuse_pcap.py [capture-N]``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))                       # rtw88_pcap_replay (codec)
sys.path.insert(0, str(REPO / "scripts" / "rtl8814au_dkms"))    # verify_pcap (DEV_ADDR)

import rtw88_pcap_replay as rp  # noqa: E402
import verify_pcap as vp  # noqa: E402

from wifit3.chips.rtl8814au_dkms import efuse  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
# Expected decoded params (same physical card across all three boots).
EXP_RFE_TYPE = 1
EXP_CRYSTAL_CAP = 0x23


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"

    # The efuse read is the first vendor-register traffic in the capture; trim to the
    # chip-version read (REG_SYS_CFG1, 0xF0). The window end is generous — the port stops at
    # EFUSE access-off, so later ops are simply left unconsumed.
    print(f"Extracting EFUSE op stream from {pcap.name} (dev {vp.DEV_ADDR[name]})...")
    ops = rp.extract_ops(pcap, vp.DEV_ADDR[name], (1, 7000), start_addr=0x00F0)
    print(f"  {len(ops)} ops in the probe/efuse window")

    time.sleep = lambda *a, **k: None
    t = rp.ReplayTransport(ops)
    try:
        params = efuse.read_chip_params(t)
    except rp.Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    print(f"\nDecoded: rfe_type={params.rfe_type} crystal_cap=0x{params.crystal_cap:02x} "
          f"mac={'<parsed>' if params.mac_address else None} "
          f"chip_version=0x{params.chip_version:08x} autoload_fail={params.autoload_fail}")

    ok = True
    if params.rfe_type != EXP_RFE_TYPE:
        print(f"FAIL: rfe_type {params.rfe_type} != {EXP_RFE_TYPE}")
        ok = False
    if params.crystal_cap != EXP_CRYSTAL_CAP:
        print(f"FAIL: crystal_cap 0x{params.crystal_cap:02x} != 0x{EXP_CRYSTAL_CAP:02x}")
        ok = False
    if params.mac_address is None:
        print("FAIL: no MAC address parsed from efuse")
        ok = False
    if not ok:
        return 1

    print(f"\nPASS: port reproduced {t.i} EFUSE ops byte-for-byte; decoded params "
          f"match the BB-confirmed values (rfe_type=1, crystal_cap=0x23) + a valid MAC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
