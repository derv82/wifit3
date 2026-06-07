"""Acceptance gate: replay-diff the rtl8814au_dkms port against the cold-boot capture.

Reconstructs the exact ordered USB conversation the vendor driver had with the chip (control
reads/writes + bulk-OUT FW packets), then drives the port's implemented bring-up against a
transport that *replays the chip's recorded read responses*. Because read-modify-writes see
the real chip values, the port must emit byte-identical writes and bulk packets.

Rides the shared rtw88-family replay engine (`scripts/rtw88_pcap_replay`). PASS = the port
reproduces the capture's USB traffic from _InitPowerOn through the latest implemented
milestone -- M1 (power-on -> firmware -> FW-ready, incl. all FW packets, which verifies the
blob) through M3b (MAC + BB + RF + channel tune + InitHalDm seed + hal_init turn-on tail),
byte-for-byte.

Run: ``uv run python scripts/rtl8814au_dkms/verify_pcap.py [capture-N.pcap]``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8814au_dkms import bb, chan, dm, efuse, firmware, mac, monitor, rf  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"

# Card device address per capture (lsusb devnum); FW download lives in the
# airmon/open phase, which starts at frame 5707 in every capture.
DEV_ADDR = {"capture-1": 51, "capture-2": 53, "capture-3": 54}
WINDOW = (5707, 30000)  # M1 + M2a + M2b (BB ~11318) + M2c (RF radio tables)
START_ADDR = 0x10C2  # first register _InitPowerOn_8814AU touches
EFUSE_WINDOW = (1, 7000)        # probe phase (precedes _InitPowerOn)
EFUSE_START_ADDR = 0x00F0       # chip-version read (REG_SYS_CFG1)


def _read_efuse_params(pcap, dev):
    """Replay the probe-phase efuse read to recover the real chip params.

    The init (M1+) window starts at _InitPowerOn, so the efuse read precedes it. Replaying it
    here means M2b+ consumes the actual efuse-decoded rfe_type / crystal_cap / tx_power
    instead of hardcoded values (the read itself is checked byte-for-byte by
    verify_efuse_pcap.py)."""
    ops = rp.extract_ops(pcap, dev, EFUSE_WINDOW, start_addr=EFUSE_START_ADDR)
    return efuse.read_chip_params(rp.ReplayTransport(ops))


def verify_monitor_block(ops) -> tuple:
    """Targeted diff of the monitor opmode entry (M3b-2).

    wifit3 enters monitor directly, so it does NOT replay airmon's STA->monitor dance that the
    cold-boot pcap shows between the hal_init turn-on tail (M3b-1) and the actual monitor
    opmode entry. The contiguous differ therefore stops at M3b-1; the monitor entry is
    verified here as a standalone 10-op block.

    Anchor on the single monitor RCR write (W REG_RCR=RCR_MONITOR_VALUE); the block is the 6
    reads (Set_MSR read + RCR/RXFLTMAP0/1/2 backups) before it, that write, and the 3 RXFLTMAP
    writes after it. Replaying monitor.enter_monitor against just those ops proves the port
    emits them byte-for-byte.
    """
    from wifit3.chips.rtl8814au_dkms import constants as C
    k = next((i for i, o in enumerate(ops)
              if o["kind"] == "W" and o["addr"] == C.REG_RCR
              and o["value"] == C.RCR_MONITOR_VALUE), None)
    if k is None:
        raise rp.Divergence("monitor RCR write (0x608=0x90003b2f) not found in capture")
    block = ops[k - 6:k + 4]            # Set_MSR(2) + 4 backups + RCR + 3 RXFLTMAP
    t = rp.ReplayTransport(block)
    monitor.enter_monitor(t)
    if t.i != len(block):
        raise rp.Divergence(f"monitor block: port emitted {t.i} of {len(block)} ops")
    return block[0]["frame"], block[-1]["frame"], len(block)


def run(cap: str | None = None) -> int:
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    fw = FW_BIN.read_bytes()
    time.sleep = lambda *a, **k: None  # replay needs no real delays

    p = _read_efuse_params(pcap, DEV_ADDR[name])
    print(f"Efuse params: rfe_type={p.rfe_type} crystal_cap=0x{p.crystal_cap:02x}")

    print(f"Extracting USB op stream from {pcap.name} (dev {DEV_ADDR[name]})...")
    ops = rp.extract_ops(pcap, DEV_ADDR[name], WINDOW, start_addr=START_ADDR)
    n_bulk = sum(1 for o in ops if o["kind"] == "B")
    print(f"  {len(ops)} ops in M1 window ({n_bulk} firmware packets)")

    t = rp.ReplayTransport(ops)
    try:
        ready = firmware.bring_up(t, fw)   # M1: power-on -> FW download -> ready
        if ready:
            mac.phy_mac_config(t)          # M2a: MAC register table
            mac.mac_init_misc(t)           # M2b: hal_init MISC stage
            bb.phy_bb_config(t, p.rfe_type, p.crystal_cap)  # M2b: PHY_BBConfig8814
            rf.phy_rf_config(t, p.rfe_type)                 # M2c: PHY_RFConfig8814A
            # The 5G TX-power / BB-swing are unused on the 2.4 GHz init tune (the band
            # switch to 5G only happens on a runtime 5G hop); passed for the signature.
            chan.init_tune(t, 1, p.tx_power, p.tx_power_5g, p.bb_swing, p.bb_swing_5g)  # M2d/M2e
            dm.init_hal_dm(t)                               # M3a: MISC11 + InitHalDm seed
            chan.set_rfe_reg_init(t, p.rfe_type)            # M3b-1: PHY_SetRFEReg8814A(TRUE)
            mac.hal_init_turn_on(t, p.mac_address)          # M3b-1: turn-on tail + MAC addr
        contiguous = t.i
        # M3b-2 (monitor opmode entry) is verified out-of-line: wifit3 enters monitor
        # directly and skips airmon's STA->monitor dance, so it is not contiguous with
        # M3b-1 on the wire. See verify_monitor_block.
        mon = verify_monitor_block(ops) if ready else None
    except rp.Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    if not ready:
        print("\nFAIL: bring_up did not reach CPU_DL_READY against the capture")
        return 1
    print(f"\nPASS: port reproduced {contiguous} USB ops byte-for-byte through M3b-1 "
          f"({n_bulk} FW packets / {len(fw)} B blob; MAC + MISC + BB + RF + channel "
          f"tune + TX power + InitHalDm phydm seed + hal_init turn-on tail "
          f"(RFE-true, NAV, MAC addr), ch1 @ 20 MHz).")
    print(f"      M3b-2 monitor opmode entry verified byte-for-byte as a {mon[2]}-op "
          f"block (Set_MSR(NOLINK) + RCR/RXFLTMAP accept-all) at frames "
          f"{mon[0]}-{mon[1]}; airmon's STA->monitor ops in between are intentionally "
          f"not replayed (wifit3 is always-monitor).")
    print(f"      {len(ops) - contiguous} later-milestone ops remain in the capture.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
