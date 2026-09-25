"""Acceptance check: replay the power-on + single-FW open prefix.

On a warm chip the vendor runs power-on + exactly one FW download at
init (no probe FW#1 / hidden report / power-off sandwich: those live at
modprobe time, and the hidden report is descriptive-only). This walks
the port's own ``power.power_on`` + ``check_powered`` + ``llt`` +
``download_firmware`` against a warm-reference capture's prefix.

Run: uv run python scripts/chips/rtl8188ftv_dkms/verify_warm.py [pcap]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv_dkms import firmware, llt, power

BUNDLES = (
    REPO / "driver_captures" / "captures_8188fu" / "capture-warm.pcap",
    REPO / "src" / "wifit3" / "scripts" / "captures_rtl8188fu"
    / "capture-1.pcap",
)


def _resolve(arg: str | None) -> Path:
    if arg is not None:
        p = Path(arg)
        if p.exists():
            return p
        raise SystemExit(f"capture not found: {arg}")
    for cand in BUNDLES:
        if cand.exists():
            return cand
    raise SystemExit("warm reference not found; pass a pcap path")


def main() -> int:
    pcap = _resolve(sys.argv[1] if len(sys.argv) > 1 else None)
    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    blob = firmware.load_firmware_blob()
    t = rp.ReplayTransport(ops)
    try:
        assert power.power_on(t) is True
        power.check_powered(t)
        assert llt.init_llt(t) is True
        llt.enable_tx_report(t)
        ver = firmware.download_firmware(t, blob)
        assert ver == (4, 0, 0x88F1), ver
    except rp.Divergence as e:
        print(f"  FAIL: {e}")
        return 1
    print(f"warm: power-on + single FW matched to op#{t.i} "
          f"(frame {ops[t.i - 1]['frame']}) from {pcap.name}")
    print("warm: green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
