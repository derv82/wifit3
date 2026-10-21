from __future__ import annotations

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
from wifit3.chips.rtl8192fu.chip import identify_chip
from wifit3.chips.rtl8192fu.efuse import parse_efuse, read_efuse
from wifit3.chips.rtl8192fu.firmware import load_firmware_blob, parse_firmware
from wifit3.chips.rtl8192fu.iqk import phy_iq_calibrate
from wifit3.chips.rtl8192fu.runtime import enable_hardware_led, enable_rf, open_rx_filters

CAP_DIR = REPO / "src/wifit3/scripts/captures_rtl8192fu"


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
    except (rp.Divergence, ValueError, IOError) as error:
        print(f"FAIL at op {replay.i}: {error}")
        return 1

    print(
        f"PASS: {replay.i} ops — identify + EFUSE + power + firmware + MAC/BB/RF + FIFO/RX/MAC policy; "
        f"cut={identity.chip_cut} vendor={identity.vendor} "
        f"MAC={efuse.mac_address.hex(':')} RFE={efuse.rfe_type} "
        f"max_bw={feature.max_bandwidth}MHz"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
