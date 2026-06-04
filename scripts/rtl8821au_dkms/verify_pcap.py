"""Acceptance gate: replay-diff the M1 bring-up against the cold-boot capture.

Drives `firmware.bring_up` against `scripts/rtw88_pcap_replay.ReplayTransport`,
which replays the chip's recorded reads and checks every write + FW page-write
byte-for-byte. PASS = the port reproduces the capture's USB conversation from
power-on (card_enable_flow) through FW-ready, incl. all firmware page writes — no
hardware required.

Run: uv run python scripts/rtl8821au_dkms/verify_pcap.py [capture-1|2|3]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821au_dkms import bb, firmware, mac, rf  # noqa: E402

CRYSTAL_CAP = 0x27   # AWUS036ACS efuse value (wire-verified)  TODO(efuse): read from EFUSE

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8821au"
DEV_ADDR = {"capture-1": 39}      # lsusb devnum; capture-2/3 TBD
WINDOW = (2523, 9302)             # airmon-ng start phase (power-on + FW upload)
START_ADDR = 0x0005               # first card_enable_flow access (CARDDIS_TO_CARDEMU)

_NOOP = lambda *a, **k: None      # replay needs no real settle delays  # noqa: E731


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if name not in DEV_ADDR:
        print(f"FAIL: unknown device address for {name}")
        return 1
    fw = firmware.load_firmware_blob()
    print(f"FW blob: {len(fw)} bytes (body {len(fw) - 32})")

    ops = rp.extract_ops(pcap, DEV_ADDR[name], WINDOW, start_addr=START_ADDR)
    n_w = sum(1 for o in ops if o["kind"] == "W")
    n_r = sum(1 for o in ops if o["kind"] == "R")
    print(f"{len(ops)} ops in window from 0x{START_ADDR:04x} ({n_r} reads, {n_w} writes)")

    t = rp.ReplayTransport(ops)
    try:
        ready = firmware.bring_up(t, fw, delay=_NOOP)   # M1
        if not ready:
            print("\nFAIL: bring_up did not reach FW-ready (WINTINI_RDY) against the capture")
            return 1
        m1_ops = t.i
        mac.phy_mac_config(t)                           # M2: MAC_REG table
        mac.mac_init_misc(t)                            # M2: queue/MISC + REG_CR
        m2_ops = t.i
        bb.phy_bb_config(t, crystal_cap=CRYSTAL_CAP)    # M3: BB PHY_REG + AGC + xtal
        rf.phy_rf_config(t)                             # M3: RadioA
    except rp.Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    print(f"\nPASS: reproduced {t.i} USB ops byte-for-byte through M3.")
    print(f"      M1={m1_ops}  M2={m2_ops - m1_ops}  M3={t.i - m2_ops} ops.")
    print(f"      {len(ops) - t.i} later-milestone ops remain in the window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
