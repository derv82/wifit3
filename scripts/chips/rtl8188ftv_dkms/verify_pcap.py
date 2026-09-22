"""Acceptance gate: replay-diff the rtl8188ftv_dkms port against its vendor cold-boot captures.

Single monotonic walk per capture, fail-closed. Milestones land here in pcap
order; each handler is the port's real bring-up code running against
``ReplayTransport``.

Capture caveat: capture-1's first frame is the power flow — tshark was still
starting through enumeration + probe, so the probe reads (chip-version
``read32(REG_SYS_CFG)`` + the EFUSE map) verify only against capture-2, whose
plug prefix is intact (``_wait_for_dump`` gate + enumeration check).

Run: uv run python scripts/porting/verify_pcap.py rtl8188ftv_dkms [capture-1|capture-2]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv_dkms import c2h, firmware, info, llt, mac, power, prom
from wifit3.chips.rtl8188ftv_dkms import bb as bb_mod
from wifit3.chips.rtl8188ftv_dkms import txpower as txpower_mod
from wifit3.chips.rtl8188ftv_dkms import chan as chan_mod
from wifit3.chips.rtl8188ftv_dkms import misc as misc_mod
from wifit3.chips.rtl8188ftv_dkms import queues as queues_mod
from wifit3.chips.rtl8188ftv_dkms import rf as rf_mod
from wifit3.chips.rtl8188ftv_dkms import constants as C
from wifit3.chips.rtl8188ftv_dkms.info import VENDOR_SMIC

CAP_DIR = REPO / "driver_captures" / "captures_8188fu"
DEFAULT_CAP = CAP_DIR / "capture-2.pcap"
FW_BLOB = CAP_DIR / "firmware" / "rtl8188fufw.bin"

_M3_OPS = 34          # record: capture-1 power flow width (poll counts vary)


def _resolve(capture: str | None) -> Path:
    if capture is None:
        return DEFAULT_CAP
    p = Path(capture)
    if p.exists():
        return p
    cand = CAP_DIR / f"{capture}.pcap"
    if cand.exists():
        return cand
    raise SystemExit(f"capture not found: {capture}")


def _walk_m3(ops, start: int) -> int:
    """Replay M3 power_on at ``start``; return the frontier index."""
    t = rp.ReplayTransport(ops[start:])
    assert power.power_on(t) is True
    frontier = start + t.i
    print(f"  PASS M3 power_on ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_fw(t, blob: bytes, label: str) -> tuple[int, int, int]:
    """Replay one FW download; return the decoded header (ver, subver, sig)."""
    version = firmware.download_firmware(t, blob)
    assert version == (4, 0, 0x88F1), version
    print(f"  PASS M4 fw {label}: ver 4 sub 0 sig 0x88f1 ({t.i} ops so far)")
    return version


def _walk_open_fw(ops, start: int, blob: bytes, label: str) -> int:
    """hal_init prologue (power-check reads) + LLT + MISC01 + FW + ready."""
    t = rp.ReplayTransport(ops[start:])
    power.check_powered(t)
    assert llt.init_llt(t) is True
    llt.enable_tx_report(t)
    _walk_fw(t, blob, label)
    return start + t.i


def _walk_m5a(ops, start: int) -> int:
    """Antenna selection + MAC table."""
    t = rp.ReplayTransport(ops[start:])
    mac.init_antenna_selection(t)
    mac.mac_config(t)
    frontier = start + t.i
    print(f"  PASS M5a antenna + MAC table ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_probe_params(ops, blob: bytes):
    """M1 + M2 wire + parses on a fresh cursor; return (idx, params, table)."""
    t = rp.ReplayTransport(ops)
    version = info.read_chip_version(t)
    assert (version.test_chip, version.vendor, version.cut, version.rf_paths) == \
        (False, VENDOR_SMIC, 1, 1), version
    print(f"  PASS M1 chip version (cut {version.cut}, SMIC, 1T1R)")
    params, table = prom.read_adapter_info(t, info.is_smic(version))
    assert params.eeprom_size == 4, params
    assert table[0:2] == b"\x29\x81", table[0:2]   # RTL_EEPROM_ID, little-endian
    assert params.mac == bytes.fromhex("44efbf1f9dfb"), params.mac.hex()
    assert (params.vid, params.pid) == (0x0BDA, 0xF179), (params.vid, params.pid)
    assert params.chplan == 0x20, hex(params.chplan)
    assert not params.chplan_disable_sw
    print(f"  PASS M2 efuse map ({t.i} ops): ID 0x8129, MAC {params.mac.hex(':')}, "
          f"VID:PID {params.vid:04x}:{params.pid:04x}, chplan 0x20, "
          f"thermal 0x{params.thermal:02x}, crystal 0x{params.crystal:02x}, "
          f"customer 0x{params.customer:02x}, kfree_flag 0x{params.kfree_flag:02x}")
    return t, params, table


def _walk_probe(ops, blob: bytes) -> tuple[int, object, bytes]:
    """M1 + M2 wire + parses + M3 (probe power) + C2H + FW#1 from op 0."""
    t, params, table = _walk_probe_params(ops, blob)
    assert power.power_on(t) is True
    print(f"  PASS M3 power_on #1 ({t.i} ops so far)")
    c2h.request_hidden_report(t)
    _walk_fw(t, blob, "#1 (probe)")
    ident, report = c2h.collect_hidden_report(t)
    assert ident == C.C2H_MAC_HIDDEN_RPT, hex(ident)
    print(f"  PASS M2-tail hidden report: id 0x19, {len(report)}B "
          f"({report.hex()})")
    assert power.card_disable(t, False) is True
    print(f"  PASS M2-tail power_off ({t.i} ops so far)")
    return t.i, params, table


def _walk_m5c(ops, start: int) -> int:
    """RF config (RFENV setup + RadioA table + TxPowerTrack load)."""
    t = rp.ReplayTransport(ops[start:])
    tables = rf_mod.rf_config(t)
    assert len(tables) == 12
    frontier = start + t.i
    print(f"  PASS M5c RF config ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_m5b(ops, start: int, crystal: int) -> int:
    """BB config (enable + RF reset + PHY_REG/AGC tables + crystal)."""
    t = rp.ReplayTransport(ops[start:])
    bb_mod.bb_config(t, crystal)
    frontier = start + t.i
    print(f"  PASS M5b BB config ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_m5f_tune(ops, start: int, params, by_rate) -> tuple[int, int]:
    """Initial ch1 tune + TX power."""
    t = rp.ReplayTransport(ops[start:])
    rf_chnl_val = chan_mod.tune_20(t, 1)
    txpower_mod.set_level(t, 1, 0, params, by_rate)
    frontier = start + t.i
    print(f"  PASS M5f tune ch1 + TX power ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier, rf_chnl_val


def _walk_m5e(ops, start: int) -> int:
    """Beacon params + burst + USB agg + turn-on block."""
    t = rp.ReplayTransport(ops[start:])
    misc_mod.init_beacon_params(t)
    misc_mod.init_burst(t)
    misc_mod.agg_tx_update(t)
    misc_mod.agg_rx_update(t)
    misc_mod.init_hw_led(t)
    misc_mod.drop_incorrect_bulk_out(t)
    misc_mod.mcast2uni_lifetime(t)
    misc_mod.turn_on_block(t)
    frontier = start + t.i
    print(f"  PASS M5e beacon/burst/agg/turn-on ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_m5d(ops, start: int, mac: bytes, out_ep_number: int,
              out_ep_queue_sel: int) -> int:
    """MISC02 queues/pages/filters."""
    t = rp.ReplayTransport(ops[start:])
    queues_mod.init_queue_reserved_page(t, out_ep_queue_sel)
    queues_mod.init_tx_buffer_boundary(t)
    queues_mod.init_queue_priority(t, out_ep_number, out_ep_queue_sel)
    queues_mod.init_page_boundary(t)
    queues_mod.init_transfer_page_size(t)
    queues_mod.init_driver_info_size(t)
    queues_mod.init_macaddr(t, mac)
    queues_mod.init_network_type(t)
    queues_mod.init_wmac_setting(t)
    queues_mod.init_adaptive_ctrl(t)
    queues_mod.init_edca(t)
    queues_mod.init_rate_fallback(t)
    queues_mod.init_retry_function(t)
    frontier = start + t.i
    print(f"  PASS M5d MISC02 queues ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


# capture-1 never saw the probe, so EFUSE-derived and USB-probed values pin
# the recorded card's data as test data.
RECORDED_CRYSTAL_CAP = 0x1D
RECORDED_MAC = bytes.fromhex("44efbf1f9dfb")
RECORDED_OUT_EP_NUMBER = 2
RECORDED_OUT_EP_QUEUE_SEL = 0x05


def run(capture: str | None = None, verbose: bool = False) -> int:
    pcap = _resolve(capture)
    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    blob = FW_BLOB.read_bytes()
    by_rate = txpower_mod.load_default_pg_tables()
    print(f"rtl8188ftv_dkms: {len(ops)} ops from {pcap.name} (dev {dev}), "
          f"fw {len(blob)}B")
    try:
        if pcap.name == "capture-1.pcap":
            probe_ops = rp.extract_ops(CAP_DIR / "capture-2.pcap",
                                       rp.find_card_device(CAP_DIR / "capture-2.pcap"))
            _, params, _ = _walk_probe_params(probe_ops, blob)
            assert params.mac == RECORDED_MAC
            frontier = _walk_m3(ops, 0)
            frontier = _walk_open_fw(ops, frontier, blob, "#2 (open)")
            crystal = RECORDED_CRYSTAL_CAP
            mac_addr = RECORDED_MAC
        else:
            frontier, params, _table = _walk_probe(ops, blob)
            crystal = params.crystal
            mac_addr = params.mac
            frontier = _walk_m3(ops, frontier)
            frontier = _walk_open_fw(ops, frontier, blob, "#2 (open)")
        out_ep_number = RECORDED_OUT_EP_NUMBER
        out_ep_queue_sel = RECORDED_OUT_EP_QUEUE_SEL
        frontier = _walk_m5a(ops, frontier)
        frontier = _walk_m5b(ops, frontier, crystal)
        frontier = _walk_m5c(ops, frontier)
        frontier = _walk_m5d(ops, frontier, mac_addr,
                             out_ep_number, out_ep_queue_sel)
        frontier = _walk_m5e(ops, frontier)
        frontier, _rf_chnl_val = _walk_m5f_tune(ops, frontier, params, by_rate)
        op = ops[frontier]
        print(f"  frontier: op#{frontier} opens the next milestone "
              f"({rp.ReplayTransport._fmt(op)})")
    except rp.Divergence as e:
        print(f"  FAIL: {e}")
        return 1
    except AssertionError as e:
        print(f"  FAIL assert: {e}")
        return 1
    print("rtl8188ftv_dkms: green to the frontier")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
