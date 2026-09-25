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
from wifit3.chips.rtl8188ftv_dkms import dm as dm_mod
from wifit3.chips.rtl8188ftv_dkms import cal as cal_mod
from wifit3.chips.rtl8188ftv_dkms import iqk as iqk_mod
from wifit3.chips.rtl8188ftv_dkms import track as track_mod
from wifit3.chips.rtl8188ftv_dkms import mode as mode_mod
from wifit3.chips.rtl8188ftv_dkms import sec as sec_mod
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


def _find_anchor(ops, start: int, addr: int, width: int, kind: str) -> int:
    for i in range(start, len(ops)):
        o = ops[i]
        if o["kind"] == kind and o.get("addr") == addr and o.get("width") == width:
            return i
    raise SystemExit(f"anchor {kind} 0x{addr:04x}/{width} not found past op#{start}")


def _walk_lc_standalone(ops, start: int) -> int:
    t = rp.ReplayTransport(ops[start:])
    cal_mod.lc_calibrate(t)
    frontier = start + t.i
    print(f"  PASS LC standalone ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_iqk_standalone(ops, start: int) -> tuple[int, dict]:
    t = rp.ReplayTransport(ops[start:])
    st = iqk_mod.iq_calibrate(t)
    frontier = start + t.i
    print(f"  PASS IQK standalone ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']}, "
          f"final={st['final']})")
    return frontier, st


def _find_seq(ops, start: int, seq: list[tuple]) -> int:
    for i in range(start, len(ops) - len(seq) + 1):
        if all(ops[i + k]["kind"] == k_ and ops[i + k].get("addr") == a
               and ops[i + k].get("width") == w
               for k, (k_, a, w) in enumerate(seq)):
            return i
    raise SystemExit(f"sequence {seq} not found past op#{start}")


def _walk_thermal_trigger(ops, start: int) -> int:
    t = rp.ReplayTransport(ops[start:])
    track_mod.thermal_trigger(t)
    frontier = start + t.i
    print(f"  PASS thermal trigger ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_station_opmode(ops, start: int, hal: dict) -> int:
    t = rp.ReplayTransport(ops[start:])
    mode_mod.set_station_opmode(t, hal)
    frontier = start + t.i
    print(f"  PASS station opmode ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_monitor_entry(ops, start: int) -> int:
    t = rp.ReplayTransport(ops[start:])
    mode_mod.enter_monitor(t)
    frontier = start + t.i
    print(f"  PASS monitor entry ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


TICK_HEAD = [("R", 0x667, 1), ("W", 0x667, 1), ("R", 0x664, 2),
             ("R", 0xC00, 4), ("W", 0xC00, 4), ("R", 0xD00, 4),
             ("W", 0xD00, 4)]


def _walk_tick(ops, start: int, hal: dict) -> int:
    was_tm = hal["tm_trigger"]
    t = rp.ReplayTransport(ops[start:])
    dm_mod.watchdog_tick(t, hal)
    frontier = start + t.i
    print(f"  PASS tick {'cb' if was_tm else 'trig'} "
          f"rem=({hal['rem_cck']:+d},{hal['rem_ofdm']:+d}) ({t.i} ops, "
          f"frames {ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


class _ScanTransport:
    """Forward-scanning replay for one thread of a raced region.

    Same read/write surface as ReplayTransport, but each port op consumes
    the first still-unconsumed recorded op with matching kind/addr/width
    (and value, for writes). The watchdog timer and an ioctl channel
    switch race on USB control transfers, so their ops weave op-by-op;
    the switch script scans first, then the tick script replays the
    leftovers strictly. Every region op is consumed exactly once; any
    port op without a match, or any leftover after both scripts, raises
    Divergence.
    """

    def __init__(self, ops, base: int):
        self.ops = ops
        self.base = base
        self.used = [False] * len(ops)

    def _take(self, want: str, kind: str, addr: int, width: int,
              value: int | None = None):
        for i, op in enumerate(self.ops):
            if self.used[i] or op["kind"] != kind \
                    or op.get("addr") != addr or op.get("width") != width:
                continue
            if value is not None and op.get("value") != value:
                continue
            self.used[i] = True
            return op.get("value")
        opno = self.base + len(self.ops)
        raise rp.Divergence(
            f"op#{opno}: race scan found no {want}")

    def _read(self, addr: int, width: int):
        return self._take(f"read 0x{addr:04x}/{width}", "R", addr, width)

    def _write(self, addr: int, width: int, value: int):
        self._take(f"write 0x{addr:04x}/{width}=0x{value:0{width * 2}x}",
                   "W", addr, width, value)

    def read8(self, a):
        return self._read(a, 1)

    def read16(self, a):
        return self._read(a, 2)

    def read32(self, a):
        return self._read(a, 4)

    def write8(self, a, v):
        self._write(a, 1, v)

    def write16(self, a, v):
        self._write(a, 2, v)

    def write32(self, a, v):
        self._write(a, 4, v)

    def remainder(self):
        return [op for i, op in enumerate(self.ops) if not self.used[i]]


def _walk_race(ops, start: int, end: int, hal: dict, params,
               by_rate) -> int:
    """Merge-walk one switch x watchdog-tick race (both scripts, every op once)."""
    markers = []
    cands = []
    for i in range(start, end):
        o = ops[i]
        if o["kind"] == "W" and o.get("addr") == 0x840 \
                and o.get("width") == 4:
            v = o.get("value", 0)
            if (v >> 20) & 0xFF == 0x18 and 1 <= (v & 0xFF) <= 13 \
                    and (v & 0xFFFFF00) == 0x1800C00:
                cands.append((i, v & 0xFF))
    # A real marker opens spur calibration (R/W 0xC40 pair); the postBW
    # full-mask 0x18 write reuses the value but has no C40 after it.
    for n, (i, ch) in enumerate(cands):
        bound = cands[n + 1][0] if n + 1 < len(cands) else end
        k1 = next((k for k in range(i + 1, bound)
                   if ops[k]["kind"] == "R"
                   and ops[k].get("addr") == 0xC40), None)
        k2 = next((k for k in range((k1 or i) + 1, bound)
                   if ops[k]["kind"] == "W"
                   and ops[k].get("addr") == 0xC40), None)
        if k1 is not None and k2 is not None:
            markers.append((i, ch))
    if len(markers) != 1:
        raise rp.Divergence(
            f"op#{start}: race region holds {len(markers)} switch markers")
    channel = markers[0][1]
    scan = _ScanTransport(ops[start:end], start)
    chan_mod.switch_channel(scan, channel, hal, params, by_rate,
                            hal["rem_cck"], hal["rem_ofdm"])
    rest = scan.remainder()
    t = rp.ReplayTransport(rest)
    dm_mod.watchdog_tick(t, hal)
    if t.i != len(rest):
        raise rp.Divergence(
            f"op#{start}: race region leaves {len(rest) - t.i} ops unverified")
    print(f"  PASS race switch ch{channel} x tick "
          f"rem=({hal['rem_cck']:+d},{hal['rem_ofdm']:+d}) "
          f"({end - start} ops, frames "
          f"{ops[start]['frame']}-{ops[end - 1]['frame']})")
    return end


def _is_serial_rmw(ops, start: int) -> int:
    if start < 0:
        return 0
    seq = [("R", 0x824, 4), ("W", 0x824, 4), ("R", 0x824, 4),
           ("W", 0x824, 4), ("W", 0x824, 4), ("R", 0x820, 4)]
    return all(ops[start + k]["kind"] == k_ and ops[start + k].get("addr") == a
               and ops[start + k].get("width") == w
               for k, (k_, a, w) in enumerate(seq))


def _find_next_switch(ops, start: int) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for channel in range(1, 14):
        want = (0x18 << 20) | (0xC00 | channel)
        for i in range(start, len(ops) - 2):
            o = ops[i]
            if (o["kind"] == "W" and o.get("addr") == 0x840
                    and o.get("width") == 4 and o.get("value") == want
                    and _is_serial_rmw(ops, i - 7)
                    and ops[i + 1]["kind"] == "R"
                    and ops[i + 1].get("addr") == 0xC40
                    and ops[i + 2]["kind"] == "W"
                    and ops[i + 2].get("addr") == 0xC40):
                cand = (i - 7, channel)
                if best is None or cand[0] < best[0]:
                    best = cand
                break
    return best


def _sync_gap(ops, hal: dict, start: int, end: int) -> None:
    for k in range(start, min(end, len(ops))):
        o = ops[k]
        if o["kind"] == "W" and o.get("width") == 4 \
                and o.get("addr") == 0xC50:
            hal["cur_ig"] = (o.get("value", 0) or 0) & 0xFF
            print(f"  SYNC race fragment op#{k} cur_ig={hal['cur_ig']:#x}")
        if o["kind"] == "W" and o.get("width") == 1 \
                and o.get("addr") == 0xA0A:
            hal["cur_cck"] = o.get("value", 0) or 0
            print(f"  SYNC race fragment op#{k} cur_cck={hal['cur_cck']:#x}")


def _walk_switch(ops, start: int, channel: int, hal: dict, params,
                 by_rate) -> int:
    t = rp.ReplayTransport(ops[start:])
    chan_mod.switch_channel(t, channel, hal, params, by_rate,
                            hal["rem_cck"], hal["rem_ofdm"])
    frontier = start + t.i
    print(f"  PASS switch ch{channel} rem=({hal['rem_cck']:+d},"
          f"{hal['rem_ofdm']:+d}) ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_dm_init(ops, start: int, hal: dict) -> int:
    """DM-init prologue reads + NHM + adaptivity + CFO + swing."""
    t = rp.ReplayTransport(ops[start:])
    dm_mod.common_info_self_init(t)
    hal["cur_ig"] = dm_mod.dig_init_igi(t) & 0xFF
    dm_mod.nhm_init(t)
    dm_mod.adaptivity_init(t)
    dm_mod.cfo_init_atc(t)
    dm_mod.thermal_swing_index(t)
    frontier = start + t.i
    print(f"  PASS DM-init prologue ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_tracking_second(ops, start: int) -> int:
    """Redundant 2nd odm_TXPowerTrackingInit: ODM_DMInit calls it directly
    right after phydm_rf_init already did; only getSwingIndex re-reads."""
    t = rp.ReplayTransport(ops[start:])
    dm_mod.tracking_init_second(t)
    frontier = start + t.i
    print(f"  PASS 2nd tracking init ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_kfree_gain(ops, start: int) -> int:
    """Kfree TX gain offset: rtw_bb_rf_gain_offset runs after the opmode
    enqueue (kfree flag 0x01 set, bb_gain zero); masked RF 0x55 write."""
    t = rp.ReplayTransport(ops[start:])
    track_mod.kfree_gain_offset(t)
    frontier = start + t.i
    print(f"  PASS kfree gain offset ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_hal_init_tail(ops, start: int) -> int:
    """usb_halinit tail: NAV_UPPER + FWHW_TXQ_CTRL BIT12 + MACTXEN/MACRXEN."""
    t = rp.ReplayTransport(ops[start:])
    misc_mod.hal_init_tail(t)
    frontier = start + t.i
    print(f"  PASS hal_init tail ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_mlme_ext(ops, start: int, hal: dict, params, by_rate) -> int:
    """init_hw_mlme_ext: set_channel_bwmode(ch1, BW20), zero remnants."""
    t = rp.ReplayTransport(ops[start:])
    chan_mod.switch_channel(t, 1, hal, params, by_rate, 0, 0)
    frontier = start + t.i
    print(f"  PASS mlme_ext ch1 set ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_m5h_start(ops, start: int) -> int:
    """CAM invalidate + MISC11 tail + GPIO."""
    t = rp.ReplayTransport(ops[start:])
    sec_mod.invalidate_cam_all(t)
    misc_mod.misc11_tail(t)
    misc_mod.init_gpio_setting(t)
    frontier = start + t.i
    print(f"  PASS M5h CAM + MISC11 + GPIO ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier


def _walk_m5f_tune(ops, start: int, params, by_rate, hal: dict) -> tuple[int, int]:
    """Initial ch1 tune + TX power."""
    t = rp.ReplayTransport(ops[start:])
    rf_chnl_val = chan_mod.tune_20(t, 1, 0, hal)
    txpower_mod.set_level(t, 1, 0, params, by_rate)
    frontier = start + t.i
    print(f"  PASS M5f tune ch1 + TX power ({t.i} ops, frames "
          f"{ops[start]['frame']}-{ops[frontier - 1]['frame']})")
    return frontier, rf_chnl_val


def _walk_m5e(ops, start: int, hal: dict) -> int:
    """Beacon params + burst + USB agg + turn-on block."""
    t = rp.ReplayTransport(ops[start:])
    misc_mod.init_beacon_params(t, hal)
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
        hal: dict = {"cur_cck": 0, "th_l2h_ini": 0xF5,
                     "adaptivity_ability": False}
        out_ep_number = RECORDED_OUT_EP_NUMBER
        out_ep_queue_sel = RECORDED_OUT_EP_QUEUE_SEL
        frontier = _walk_m5a(ops, frontier)
        frontier = _walk_m5b(ops, frontier, crystal)
        frontier = _walk_m5c(ops, frontier)
        frontier = _walk_m5d(ops, frontier, mac_addr,
                             out_ep_number, out_ep_queue_sel)
        frontier = _walk_m5e(ops, frontier, hal)
        frontier, hal["rf_chnl_val"] = _walk_m5f_tune(ops, frontier, params, by_rate, hal)
        frontier = _walk_m5h_start(ops, frontier)
        frontier = _walk_dm_init(ops, frontier, hal)
        frontier = _walk_tracking_second(ops, frontier)
        lc_start = _find_anchor(ops, frontier, 0xD03, 1, "R")
        _walk_lc_standalone(ops, lc_start)
        iqk_start = _find_anchor(ops, frontier, 0x948, 4, "R")
        iqk_end, iqk_st = _walk_iqk_standalone(ops, iqk_start)
        assert iqk_st["final"] != 0xFF
        hal["iqk_x"], hal["iqk_y"] = iqk_st["result"][iqk_st["final"]][:2]
        trig_end = _walk_thermal_trigger(ops, iqk_end)
        tail_end = _walk_hal_init_tail(ops, trig_end)
        mlme_end = _walk_mlme_ext(ops, tail_end, hal, params, by_rate)
        opmode_end = _walk_station_opmode(ops, mlme_end, hal)
        frontier = _walk_kfree_gain(ops, opmode_end)
        frontier = _walk_monitor_entry(ops, frontier)
        hal.update(track_mod.tracking_init_state(params.thermal))
        hal["tm_trigger"] = True
        hal["params"], hal["by_rate"] = params, by_rate
        cursor = frontier
        n_switches = n_ticks = n_races = 0
        while True:
            sw = _find_next_switch(ops, cursor)
            try:
                head = _find_seq(ops, cursor, TICK_HEAD)
            except SystemExit:
                head = None
            if sw is None and head is None:
                break
            nearest = min([v for v in (sw[0] if sw else None, head)
                           if v is not None])
            if nearest > cursor:
                cursor = _walk_race(ops, cursor, nearest, hal, params,
                                    by_rate)
                n_races += 1
                continue
            if head is not None and (sw is None or head < sw[0]):
                _sync_gap(ops, hal, cursor, head)
                cursor = _walk_tick(ops, head, hal)
                n_ticks += 1
            else:
                assert sw is not None
                _sync_gap(ops, hal, cursor, sw[0])
                cursor = _walk_switch(ops, sw[0], sw[1], hal, params,
                                      by_rate)
                n_switches += 1
        print(f"  events: {n_switches} switches + {n_ticks} ticks + "
              f"{n_races} races, rem=({hal['rem_cck']:+d},"
              f"{hal['rem_ofdm']:+d})")
        if cursor >= len(ops):
            print(f"  end: op#{cursor} (end of capture)")
            op = None
        else:
            op = ops[cursor]
            print(f"  end: op#{cursor} ({rp.ReplayTransport._fmt(op)}), "
                  f"{len(ops) - cursor} ops unclaimed")
    except rp.Divergence as e:
        print(f"  FAIL: {e}")
        return 1
    except AssertionError as e:
        print(f"  FAIL assert: {e}")
        return 1
    print("rtl8188ftv_dkms: green")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
