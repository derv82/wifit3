"""Byte-for-byte replay-diff of the rtl8822bu_dkms port vs the morrownr rtl88x2bu
cold-boot capture.

The 8822b transport is dev-centric (it calls ``dev.ctrl_transfer`` so it can emit
the 0x4E0 page-switch mirror), so the gate replays at the ctrl_transfer layer:
``extract_ctrl_ops`` + ``rtw88_pcap_replay.ReplayDevice`` feed recorded reads back
and byte-check every write (mirror included). One monotonic cursor walks the whole
capture; the first op the port does NOT reproduce is the frontier — the next thing
to port.

    uv run python scripts/verify_pcap.py rtl8822bu_dkms
    uv run python scripts/rtl8822bu_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import bb, chipid, efuse, firmware, mac, phy_cond, rf, usbphy  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import constants as const  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_rtl88x2bu" / "capture-1.pcap"

# The deterministic table-driven cold init ends here; everything after is the RF cal scan (the
# vendor pre-cals every channel in both bands). We reproduce the init in full and stop at this
# boundary — the cal is done per-channel on-demand by set_channel, not by replaying the scan.
CAL_SCAN_START = 9400


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


def _bringup(t) -> None:
    """The ported bring-up so far, driven against the replay device. Each milestone
    appends here; the gate advances to the next unaccounted op."""
    info = chipid.get_chip_info(t)         # M0: HALMAC chip-id/cut (R 0xFC, R 0xF1)
    usbphy.phy_cfg_usb(t, info.chip_ver)   # M0: USB3 intf-phy param (W 0xff0d/0e/0c)
    chipid.read_chip_version(t)            # M0: rtw chip-version (R 0xF0/0xF4/0x68)
    e = efuse.read_efuse(t)                # M1: HALMAC physical EFUSE dump (R 0x0A, 0x30 loop)
    mac.power_on(t, info.chip_ver)         # M2: pre_init + card_en pwr-seq + init_system_cfg
    # hal_read_mac_hidden_rpt: request the FW report, then (below) download FW + send info + read it
    t.write8(const.REG_C2HEVT_MSG_NORMAL, const.C2H_DEFEATURE_RSVD)
    firmware.download(t, firmware.load_firmware_blob())   # M3: HALMAC iDDMA FW upload
    mac.init_mac_cfg(t)                    # M4: init_mac_cfg — trx + protocol + edca + wmac RX
    mac.init_mac_flow_tail(t)              # M4: RCR sync + RTS-full-bw + USB RX aggregation
    alloc = mac.set_trx_fifo_info()        # M4: _send_general_info — H2C packets + H2CQ readback
    firmware.send_general_info(t, e.rfe_type, info.chip_ver,
                               alloc.rsvd_fw_txbuf_addr - alloc.rsvd_boundary,
                               alloc.rsvd_h2cq_addr)
    firmware.read_mac_hidden_rpt(t)        # M4: poll + read the FW MAC-hidden C2H report
    # --- read-chip-info tail, then the 2nd (real) init cycle: rtl8822b_hal_init ---
    t.write16(0x00AA, 0x8000)              # [WIRE] post-C2H op before the power-off (source TBD)
    mac.power_off(t, info.chip_ver)        # hal_read_mac_hidden_rpt tail: power-off (card_dis -> cold)
    efuse.read_phydm_trim(t)               # rtw_phydm_read_efuse: 3 cached PG-trim reads (R 0x35 x3)
    t.write8(0xFE58, 0x00)                 # [WIRE] RPWM clear before the 2nd power-on (source TBD)
    mac.power_on(t, info.chip_ver)         # rtl8822b_hal_init: COLD power-on (pre_init + card_en)
    # 2nd FW DL: full beacon desc; rsvd_boundary is now the real txff boundary (set in cycle 1)
    firmware.download(t, firmware.load_firmware_blob(), beacon=True, rsvd_boundary=alloc.rsvd_boundary)
    mac.init_mac_cfg(t)                    # 2nd init_mac_flow (reuse): trx + protocol + edca + wmac
    mac.init_mac_flow_tail(t)              # 2nd RCR sync + RTS-full-bw + USB RX-agg
    # _drv_enable_trx is no-wire; 2nd _send_general_info (no mac_hidden this cycle; seq reset to 0).
    # The real init's get_trx_path has the full 2T2R config: rf_type 2, ant 3/3, package 7.
    firmware.send_general_info(t, e.rfe_type, info.chip_ver,
                               alloc.rsvd_fw_txbuf_addr - alloc.rsvd_boundary, alloc.rsvd_h2cq_addr,
                               rf_type=2, rf_type_drv=2, tx_ant=3, rx_ant=3, package_type=7)
    mac.init_mac_register(t)               # rtl8822b_phy_init_mac_register: PHYDM MAC-reg table
    mac.config_rx_info(t)                  # cfg_drv_info(PHY_STATUS): DRVINFO sz + RCR app-physts
    mac.enable_bb_rf(t, e.log_map[0xCA])   # set_hw_value(EN_BB_RF): turn on BB/RF clocks
    bb.phy_parameter_init(t, post=False)   # PHYDM PRE_SETTING: 0x808 OFDM/CCK block off
    bb.phy_bb_config(t)                     # BB phy-reg table (2T2R baked in) — odm_config_bb(PHY_REG)
    cfg = phy_cond.PhyCondConfig(cut=info.chip_ver, rfe=e.rfe_type, package=7)
    bb.phy_agc_config(t, cfg)              # BB AGC table — odm_config_bb_agc (cut/rfe walker)
    bb.set_crystal_cap(t, e.crystal_cap)   # init_bb_reg tail: xtal-cap into 0x24[30:25]/0x28[6:1]
    rf.phy_rf_config(t, cfg)               # init_rf_reg: RF-A then RF-B radio tables (cut/rfe walker)
    bb.phy_parameter_init(t, post=True)    # PHYDM POST_SETTING: 0x808 OFDM/CCK block on


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None       # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    # Merge control + bulk-OUT into one frame-ordered stream so the FW download (vendor
    # register writes interleaved with bulk FW packets) replays against one ReplayDevice.
    ctrl = rp.extract_ctrl_ops(pcap, dev_addr)
    bulk = rp.extract_bulk_out_ops(pcap, dev_addr)
    ops = rp.merge_ops_by_frame(ctrl, bulk)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ctrl)} control + {len(bulk)} bulk-OUT ops")
    print("  first 40 control ops (* = 0x4E0 page-switch mirror):")
    for k, o in enumerate(ops[:40]):
        tag = " *" if o["wval"] == 0x04E0 else ""
        print(f"    [{k:3}] f{o['frame']:<7} {_fmt(o)}{tag}")

    dev = rp.ReplayDevice(ops)
    t = Rtl8822buTransport(dev)
    try:
        _bringup(t)
    except rp.Divergence as e:
        print(f"\nDIVERGENCE after {dev.i} ops:\n  {e}")
        return 1

    consumed = dev.i
    print(f"\nported bring-up reproduced {consumed}/{len(ops)} ops clean.")
    if consumed < len(ops):
        nxt = ops[consumed]
        print(f"FRONTIER -> op #{consumed} (frame {nxt['frame']}): {_fmt(nxt)}")
        if consumed >= CAL_SCAN_START:
            # The deterministic cold init is fully reproduced. Everything past here is the
            # vendor's all-channel RF cal scan (IQK/DPK/TSSI over every 2.4G+5G channel, twice);
            # per the Lead's decision we cal per-channel on-demand in set_channel, not by replaying
            # this scan. See RTL8822BU_DKMS.md "RF calibration".
            print("  PASS: deterministic cold init complete; remaining ops are the per-channel")
            print("  RF cal scan (handled on-demand by set_channel - not a monotonic replay).")
        else:
            print("  (this is the next op to port; not yet a full PASS)")
        return 0
    print("PASS: full single-cursor reproduction.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
