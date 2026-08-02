"""Acceptance gate: replay-diff the M1–M5 bring-up against the cold-boot capture.

Drives the port against `scripts/rtw88_pcap_replay.ReplayTransport`, which replays
the chip's recorded reads and checks every write + FW page-write byte-for-byte. PASS
= the port reproduces the capture's USB conversation — no hardware required.

Coverage:
  * M1–M4 contiguous from power-on (`card_enable_flow`) through the 2.4 GHz channel
    tune, on one op stream (the per-rate TX-power sweep that follows M4 on the wire is
    deferred and not emitted).
  * M5 §1 post-tune tail + §2 InitHalDm, contiguous from frame 7609, with the live
    8821a EDCCA PSD search (frames 7659–8605, reads PSD 0xFA0) skipped — that block is
    verified live by the beacon count, not the differ.
  * M5 §3 monitor opmode entry, verified out-of-line (anchored on the monitor RCR),
    since wifit3 enters monitor directly and skips airmon's STA→monitor dance.

Run: uv run python scripts/rtl8821au_dkms/verify_pcap.py [capture-1|2|3]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821au_dkms import bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rtl8821au"
DEV_ADDR = {"capture-1": 39}      # lsusb devnum; capture-2/3 TBD
WINDOW = (2523, 9302)             # airmon-ng start phase (power-on + FW upload)
START_ADDR = 0x0005               # first card_enable_flow access (CARDDIS_TO_CARDEMU)
EFUSE_WINDOW = (60, 2520)         # probe phase (chip-version read -> efuse byte loop)
EFUSE_START_ADDR = 0x00F0

# M5 lives after the (deferred) TX-power sweep; replay it as its own op stream trimmed
# to the first invalidate_cam_all (REG_CAMCMD 0x0670, frame 7609).
M5_WINDOW = (7600, 8920)
M5_START_ADDR = 0x0670
# The live 8821a EDCCA PSD search (lna-disable -> dbg-port loop -> lna-enable -> resume
# -> mac_edcca_state) spans frames 7659–8605 and reads live PSD — not byte-replayable.
# Drop it from the contiguous stream so InitHalDm's deterministic head (through the NHM
# env-monitor) is followed directly by its tail (the RX init-gain commit).
EDCCA_SKIP = (7657, 8607)         # exclusive: removes frames 7659..8605
M5_TAIL_END = 8643                # last §1b write (REG_USB_HRPWM); airmon's STA→monitor
                                  # dance + the monitor block follow and are out-of-line

_NOOP = lambda *a, **k: None      # replay needs no real settle delays  # noqa: E731


def _read_efuse_params(pcap: Path, dev: int):
    """Replay the probe-phase efuse read to recover the real chip params (crystal_cap
    + the path-A TX-power base/diffs the M-TXPWR sweep consumes). The read itself is
    checked byte-for-byte by verify_efuse_pcap.py."""
    ops = rp.extract_ops(pcap, dev, EFUSE_WINDOW, start_addr=EFUSE_START_ADDR)
    return efuse.read_chip_params(rp.ReplayTransport(ops))


def _verify_m5_tail(pcap: Path, dev: int) -> tuple:
    """Contiguous replay of M5 §1 + §2(minus the live EDCCA search) from frame 7609."""
    full = rp.extract_ops(pcap, dev, M5_WINDOW, start_addr=M5_START_ADDR)
    contiguous = [o for o in full
                  if o["frame"] <= M5_TAIL_END
                  and not (EDCCA_SKIP[0] < o["frame"] < EDCCA_SKIP[1])]
    t = rp.ReplayTransport(contiguous)
    mac.hal_init_misc_pre(t)                  # §1a: invalidate_cam_all + MISC11
    dig.init_hal_dm(t, search_edcca=False)    # §2: InitHalDm, deterministic parts only
    mac.hal_init_misc_post(t)                 # §1b: turn-on tail
    if t.i != len(contiguous):
        raise rp.Divergence(f"M5 tail: port emitted {t.i} of {len(contiguous)} ops")
    n_edcca = sum(1 for o in full if EDCCA_SKIP[0] < o["frame"] < EDCCA_SKIP[1])
    return len(contiguous), n_edcca, full


def _verify_monitor_block(ops) -> tuple:
    """Out-of-line diff of the M5 §3 monitor opmode entry.

    wifit3 enters monitor directly and skips airmon's STA→monitor dance, so this block
    is not contiguous with §1b on the wire. Anchor on the monitor RCR write
    (REG_RCR = 0x9000382F) and replay enter_monitor against the 10-op block: the 3 ops
    before it (Set_MSR read+write, RCR backup read), the write, and the 6 after it
    (RXFLTMAP0/1/2 backup reads + accept-all writes).
    """
    k = next((i for i, o in enumerate(ops)
              if o["kind"] == "W" and o["addr"] == monitor.REG_RCR
              and o["value"] == monitor.RCR_MONITOR_VALUE), None)
    if k is None:
        raise rp.Divergence("monitor RCR write (0x608=0x9000382f) not found in capture")
    block = ops[k - 3:k + 7]
    t = rp.ReplayTransport(block)
    monitor.enter_monitor(t)
    if t.i != len(block):
        raise rp.Divergence(f"monitor block: port emitted {t.i} of {len(block)} ops")
    return block[0]["frame"], block[-1]["frame"], len(block)


def run(cap: str | None = None) -> int:
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if name not in DEV_ADDR:
        print(f"FAIL: unknown device address for {name}")
        return 1
    dev = DEV_ADDR[name]
    fw = firmware.load_firmware_blob()
    print(f"FW blob: {len(fw)} bytes (body {len(fw) - 32})")

    p = _read_efuse_params(pcap, dev)
    print(f"Efuse: crystal_cap=0x{p.crystal_cap:02x} (TX-power base cck[0]=0x{p.tx_power.cck_base[0]:02x} "
          f"bw40[0]=0x{p.tx_power.bw40_base[0]:02x})")

    ops = rp.extract_ops(pcap, dev, WINDOW, start_addr=START_ADDR)
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
        jp = efuse.build_jaguar_params(p)               # phy_cond walker inputs (board_type)
        bb.phy_bb_config(t, crystal_cap=p.crystal_cap, params=jp)  # M3: BB PHY_REG + AGC + xtal
        rf.phy_rf_config(t, jp)                         # M3: RadioA
        m3_ops = t.i
        chan.set_chnl_bw(t, 1, p.bb_swing_2g, p.ext_lna_2g)  # M4: band + channel + 20 MHz BW
        m4_ops = t.i
        txpower.set_tx_power(t, 1, p.tx_power)          # M-TXPWR: per-rate txagc (7485-7607)
        mtxpwr_ops = t.i

        # M5: separate op stream (now contiguous with M4 via the TX-power block above).
        m5_det, m5_skipped, m5_full = _verify_m5_tail(pcap, dev)
        mon = _verify_monitor_block(m5_full)
    except rp.Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    print(f"\nPASS: reproduced {mtxpwr_ops} USB ops byte-for-byte through M4 + TX-power, then "
          f"{m5_det} more through M5 S1+S2.")
    print(f"      M1={m1_ops}  M2={m2_ops - m1_ops}  M3={m3_ops - m2_ops}  M4={m4_ops - m3_ops}  "
          f"TXpwr={mtxpwr_ops - m4_ops}  M5(tail)={m5_det} ops.")
    print(f"      M5 S1 post-tune tail (invalidate_cam_all + MISC11 + InitHalDm DIG/CCK-PD/NHM "
          f"+ RX-gain + turn-on) byte-exact; {m5_skipped} live EDCCA-search ops "
          f"(PSD 0xFA0, frames 7659-8605) skipped -- verified live by the beacon count.")
    print(f"      M5 S3 monitor opmode entry verified byte-for-byte as a {mon[2]}-op block "
          f"(Set_MSR(NOLINK) + RCR=0x9000382F accept-all + RXFLTMAP) at frames {mon[0]}-{mon[1]}; "
          f"airmon's STA->monitor ops in between are intentionally not replayed.")
    print(f"      {len(ops) - mtxpwr_ops} later ops remain in the window (the skipped EDCCA "
          f"search, airmon's STA dance, and runtime channel hops).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
