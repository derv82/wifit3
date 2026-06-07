"""Byte-for-byte differ for the RTL8188EUS channel tune (PHY_SwChnl + PHY_SetBWMode).

`verify_pcap.py` covers the init through the hal_init tail; the cold-boot wire then hands to
airmon-ng, whose first `iw set channel` drives a full vendor tune. This replays the port's
`chan.set_channel` against that initial channel-set window and byte-diffs it.

The window is sliced out of the wire by anchoring on the airmon RXFLTMAP1 write (`W 0x6a2`,
the last airmon-monitor-prefix op) and ending at the monitor opmode entry (`R 0x102` MSR) — so
the whole tune (TX-power re-tune + RF_CHNLBW channel write + the 20 MHz BW block + RF_CHNLBW BW
write) is diffed with no anchoring/trim inside it.

The initial set lands before the async DIG-burst watchdog ramps up, so it diffs clean. The
per-hop airodump windows (iw.log) interleave the DIG burst (FA counters 0xC00/0xD00, NHM,
EDCCA) and need that watchdog ported to filter — deferred (see RTL8188EUS_DKMS.md).

    uv run python scripts/rtl8188eus_dkms/verify_channels.py [capture-1|2|3]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus_dkms import (  # noqa: E402
    bb, chan, dm, efuse, firmware, mac, pwrseq, rf, txpower,
)
from wifit3.chips.rtl8188eus_dkms.constants import DEFAULT_INIT_CHANNEL  # noqa: E402

REG_SYS_CFG = 0x00F0
REG_RXFLTMAP1 = 0x06A2     # airmon monitor-prefix tail (last op before the channel set)
REG_MSR = 0x0102           # monitor opmode entry (first op after the channel set)
CAP_DIR = REPO / "usb_dumps_new" / "captures_8188eu"


def _strip(ops):
    return [o for o in ops
            if not (o["kind"] == "R" and o.get("addr") == REG_SYS_CFG and o["width"] == 4)]


def _replay_halinit(pcap, dev):
    """Replay the whole hal_init and return (transport, ops, params, RfRegChnlVal[A])."""
    t0 = rp.ReplayTransport(rp.extract_ops(pcap, dev, start_addr=0x0006))
    pwrseq.power_on(t0)
    params = efuse.read_chip_params(t0)
    ops = _strip(rp.extract_ops(pcap, dev, start_addr=0x0080))
    t = rp.ReplayTransport(ops)
    firmware.download_firmware(t, firmware.load_firmware_blob())
    mac.phy_mac_config(t)
    bb.phy_bb_config(t, crystal_cap=params.crystal_cap)
    rf.phy_rf_config(t)
    efuse.iol_efuse_patch(t)
    mac.init_tx_buffer_boundary(t)
    mac.init_llt(t)
    mac.init_misc02(t)
    rf_chnl = rf.read_rf_chnl_val(t)[0]
    bb.bb_turn_on_block(t)
    mac.invalidate_cam_all(t)
    txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL)
    mac.init_misc11_tail(t)
    dm.init_hal_dm(t)
    dm.init_hal_tail(t)
    return t, ops, params, rf_chnl


def verify(cap_name: str) -> int:
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / f"{cap_name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    t, ops, params, rf_chnl = _replay_halinit(pcap, dev)

    # The channel set sits between the airmon monitor-prefix (RXFLTMAP1 write) and the
    # monitor opmode entry (MSR read). airmon's first iw command tunes channel 1.
    try:
        i6a2 = next(i for i in range(t.i, len(ops))
                    if ops[i]["kind"] == "W" and ops[i].get("addr") == REG_RXFLTMAP1)
        j102 = next(i for i in range(i6a2 + 1, len(ops))
                    if ops[i]["kind"] == "R" and ops[i].get("addr") == REG_MSR)
    except StopIteration:
        print(f"{cap_name}: FAIL — could not locate the channel-set window anchors")
        return 1

    window = ops[i6a2 + 1:j102]
    tw = rp.ReplayTransport(window)
    try:
        new_rf = chan.set_channel(tw, params.tx_power, rf_chnl, 1)
    except rp.Divergence as d:
        print(f"{cap_name}: FAIL ch1 @ first divergence:\n  {d}")
        return 1
    if tw.i != len(window):
        print(f"{cap_name}: FAIL — {tw.i}/{len(window)} ops matched (window not fully consumed)")
        return 1
    print(f"{cap_name}: PASS ch1 channel tune — {tw.i} ops byte-for-byte "
          f"(RfRegChnlVal {rf_chnl:#07x} -> {new_rf:#07x})")
    return 0


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    return verify(name)


if __name__ == "__main__":
    raise SystemExit(main())
