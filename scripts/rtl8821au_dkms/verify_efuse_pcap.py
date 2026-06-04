"""Acceptance gate: replay-diff the probe-phase EFUSE read against the cold-boot pcap.

Reproduces the chip-version + autoload + EFUSE byte-loop conversation the vendor had
before _InitPowerOn, byte-for-byte, and prints the decoded params (crystal_cap, MAC,
the path-A TX-power base/diffs the M-TXPWR txagc sweep consumes).

Run: uv run python scripts/rtl8821au_dkms/verify_efuse_pcap.py [capture-1|2|3]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821au_dkms import efuse  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8821au"
DEV_ADDR = {"capture-1": 39}
WINDOW = (60, 2520)          # probe phase: chip-version read -> efuse byte loop
START_ADDR = 0x00F0          # first ReadChipVersion access


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if name not in DEV_ADDR:
        print(f"FAIL: unknown device address for {name}")
        return 1
    ops = rp.extract_ops(pcap, DEV_ADDR[name], WINDOW, start_addr=START_ADDR)
    print(f"{len(ops)} ops in the probe window from 0x{START_ADDR:04x}")

    t = rp.ReplayTransport(ops)
    try:
        p = efuse.read_chip_params(t)
    except rp.Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    print(f"\nPASS: reproduced {t.i} probe ops byte-for-byte ({len(ops) - t.i} remain).")
    print(f"  crystal_cap = 0x{p.crystal_cap:02x}  (expect 0x27)")
    print(f"  mac_address = {p.mac_address or '<blank>'}")
    print(f"  chip_version = 0x{p.chip_version:08x}  autoload_fail={p.autoload_fail}")
    tp = p.tx_power
    print(f"  2.4G CCK base  = {[hex(x) for x in tp.cck_base]}")
    print(f"  2.4G BW40 base = {[hex(x) for x in tp.bw40_base]}")
    print(f"  diffs: cck={tp.cck_diff} ofdm={tp.ofdm_diff} bw20={tp.bw20_diff}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
