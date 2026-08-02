"""Byte-for-byte gate for the INITIAL 2.4 GHz channel-set (switch_band + switch_channel + bw).

This is the one RX-path span with no standing gate: after cold init (op 9855 / frame 19965) and
the opmode block, the vendor's first tune to ch1 wires the 2.4 GHz RX path — CCK-enable 0x808[28],
the MAC/BB CCK-check 0x454/0xA80, the iFEM RFE antenna switch 0xCB0/0xCA0, 0x8CC/0x8D8 — via
config_phydm_switch_band_8822b (it runs because cold init leaves the synth in 5 GHz, so the first
2.4 GHz tune is a band change). verify_pcap stops at 9855; verify_channels only slices iw.log hops
(all prev_ch set, so no band switch). So a 2.4 GHz RX-path divergence here is invisible to both
gates. This replays chan.set_channel_bw(ch1, prev_ch=None) against the capture's initial-tune window
and reports the matched-op count + the first divergence.

Run: uv run python scripts/rtl8822bu_dkms/verify_initial_tune.py [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import chan, chipid, efuse, txpower, usbphy  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rtl88x2bu"
RF_A_0x18_READ = 0x2860         # switch_band/switch_channel open by reading RF_A 0x18 (BB 0x2800+0x18*4)
# Cold init ends op 9855 / frame 19965; monitor-enable lands f20405. The initial channel-set is the
# window between: the first RF_A-0x18 read at/after this frame begins switch_band(ch1).
INITIAL_TUNE_FROM = 19966


def verify(cap_name: str) -> int:
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / f"{cap_name}.pcap"
    dev = rp.find_card_device(pcap)
    ctrl = rp.extract_ctrl_ops(pcap, dev)

    # Decode the PG TX-power block (read during cold init) so the gate can optionally bridge into
    # the per-channel TXAGC. EFUSE prefix only (all < op 4100), before the cap-2/3 stale-global seam.
    ct = Rtl8822buTransport(rp.ReplayDevice(
        rp.merge_ops_by_frame(ctrl, rp.extract_bulk_out_ops(pcap, dev))))
    info = chipid.get_chip_info(ct)
    usbphy.phy_cfg_usb(ct, info.chip_ver)
    chipid.read_chip_version(ct)
    pg = txpower.parse_pg(efuse.read_efuse(ct).log_map)

    # Window: from just past cold init to the monitor-enable region; the replay stops where it
    # diverges (the band-set's end), so an over-long window is fine.
    win = [o for o in ctrl if o["frame"] >= INITIAL_TUNE_FROM]
    head_i = next((i for i, o in enumerate(win)
                   if o["dir"] == "IN" and o["wval"] == RF_A_0x18_READ), None)
    if head_i is None:
        print(f"{cap_name}: no RF_A-0x18 read after frame {INITIAL_TUNE_FROM} — can't locate the tune")
        return 1
    win = win[head_i:]
    print(f"{cap_name}: initial-tune window starts at frame {win[0]['frame']} "
          f"({len(win)} ctrl ops to monitor-enable region)")

    # Gate 1: band + channel + bandwidth only (txpwr_pg=None), mirroring the doc's claimed 165 ops.
    rdev = rp.ReplayDevice(win)
    t = Rtl8822buTransport(rdev)
    try:
        chan.set_channel_bw(t, 1, prev_ch=None, txpwr_pg=None)
    except rp.Divergence as d:
        print(f"  FAIL @ op {rdev.i}: set_channel_bw(ch1) diverges from the capture --\n    {d}")
        nxt = win[rdev.i] if rdev.i < len(win) else None
        if nxt:
            print(f"    capture's next op: {nxt['dir']} 0x{nxt['wval']:04x} "
                  f"(frame {nxt['frame']}) data={nxt['data'].hex() if nxt['data'] else ''}")
        return 1
    nxt = next((win[k] for k in range(rdev.i, len(win)) if win[k]["wval"] != 0x04E0), None)
    edge = f"-> next capture op 0x{nxt['wval']:04x} @f{nxt['frame']}" if nxt else "-> end"
    print(f"  PASS: switch_band(ch1)+switch_channel+bandwidth byte-for-byte ({rdev.i} ops) {edge}")
    return 0


def main() -> int:
    return verify(Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem)


if __name__ == "__main__":
    raise SystemExit(main())
