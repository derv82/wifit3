from __future__ import annotations

import bisect
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8192fu.bringup import (
    calibrate_lc,
    fifo_and_rx_policy,
    finish_cold_init,
    firmware_bringup,
    mac_policy,
    set_tx_power,
    table_bringup,
)
from wifit3.chips.rtl8192fu.chan import config_channel
from wifit3.chips.rtl8192fu.chip import identify_chip
from wifit3.chips.rtl8192fu.efuse import parse_efuse, read_efuse
from wifit3.chips.rtl8192fu.firmware import load_firmware_blob, parse_firmware
from wifit3.chips.rtl8192fu.iqk import phy_iq_calibrate
from wifit3.chips.rtl8192fu.runtime import (
    configure_monitor_filter,
    enable_hardware_led,
    enable_rf,
    open_rx_filters,
)
from wifit3.chips.rtl8192fu.rx import iter_frames
from wifit3.dot11.parser import WlanFrameParser

CAP_DIR = REPO / "src/wifit3/scripts/captures_rtl8192fu"


def _verify_channels(pcap: Path, device: int, efuse) -> bool:
    iw_log = pcap.with_name(f"{pcap.stem}_logs") / "iw.log"
    text = iw_log.read_text()
    starts = re.findall(r"\[([0-9.]+)\] Executing: .* set channel (\d+)", text)
    ends = re.findall(r"\[([0-9.]+)\] Execution completed", text)
    frame_numbers, epochs = rp.frame_epochs(pcap)
    for (start, channel_text), end in zip(starts, ends, strict=True):
        channel = int(channel_text)
        first = frame_numbers[bisect.bisect_left(epochs, float(start))]
        last_index = bisect.bisect_right(epochs, float(end)) - 1
        last = frame_numbers[last_index]
        ops = rp.extract_ops(pcap, device, (first, last))
        if not ops:
            print(f"  channel {channel:2}: no driver operations (unchanged channel)")
            continue
        replay = rp.ReplayTransport(ops)
        try:
            set_tx_power(replay, efuse, 1)
            config_channel(replay, 1)
            set_tx_power(replay, efuse, channel)
            config_channel(replay, channel)
        except rp.Divergence as error:
            print(f"FAIL channel {channel} at op {replay.i}: {error}")
            return False
        if replay.i != len(ops):
            print(f"FAIL channel {channel}: consumed {replay.i}/{len(ops)} ops")
            return False
        print(f"  channel {channel:2}: PASS ({replay.i} ops)")
    return True


def _verify_rx(pcap: Path, device: int) -> bool:
    buffers = rp.extract_bulk_in_ops(pcap, device)
    frames = 0
    packets = 0
    rssi_values: list[int] = []
    for buffer in buffers:
        for frame, rssi in iter_frames(buffer, cck_new_agc=True):
            frames += 1
            rssi_values.append(rssi)
            if WlanFrameParser.parse_80211_frame(frame, rssi) is not None:
                packets += 1
    if not frames or not packets:
        print(f"FAIL RX: {len(buffers)} buffers produced {frames} frames/{packets} packets")
        return False
    print(
        f"  RX: PASS ({len(buffers)} buffers, {frames} frames, {packets} parsed packets, "
        f"RSSI {min(rssi_values)}..{max(rssi_values)} dBm)"
    )
    return True


def run(cap: str | None = None) -> int:
    time.sleep = lambda *_args, **_kwargs: None
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    device = rp.find_card_device(pcap)
    if not rp.audit_coverage(pcap, device):
        return 1
    ops = rp.extract_ops(pcap, device)
    replay = rp.ReplayTransport(ops)
    replay.endpoints = SimpleNamespace(bulk_out=(0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D))

    try:
        identity = identify_chip(replay)
        efuse = parse_efuse(read_efuse(replay))
        feature = firmware_bringup(replay, parse_firmware(load_firmware_blob()))
        table_bringup(replay, efuse)
        fifo_and_rx_policy(replay)
        mac_policy(replay)
        set_tx_power(replay, efuse)
        calibrate_lc(replay)
        phy_iq_calibrate(replay, efuse.rfe_type)
        finish_cold_init(replay)
        enable_hardware_led(replay)
        enable_rf(replay)
        open_rx_filters(replay)
        for _ in range(3):
            configure_monitor_filter(replay)
        set_tx_power(replay, efuse, 1)
        config_channel(replay, 1)
    except (rp.Divergence, ValueError, IOError) as error:
        print(f"FAIL at op {replay.i}: {error}")
        return 1

    if not _verify_channels(pcap, device, efuse):
        return 1
    if not _verify_rx(pcap, device):
        return 1

    print(
        f"PASS: {replay.i} ops — identify + EFUSE + power + firmware + MAC/BB/RF + FIFO/RX/MAC policy + monitor; "
        f"cut={identity.chip_cut} vendor={identity.vendor} "
        f"MAC={efuse.mac_address.hex(':')} RFE={efuse.rfe_type} "
        f"max_bw={feature.max_bandwidth}MHz"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
