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
from wifit3.chips.rtl8188ftv_dkms import info, power, prom
from wifit3.chips.rtl8188ftv_dkms import constants as C
from wifit3.chips.rtl8188ftv_dkms.info import VENDOR_SMIC

CAP_DIR = REPO / "driver_captures" / "captures_8188fu"
DEFAULT_CAP = CAP_DIR / "capture-2.pcap"

_M3_OPS = 34          # power flow (31) + REG_CR dance (3)
_M2_FRONTIER = {"kind": "W", "addr": C.REG_C2HEVT_MSG_NORMAL,
                "width": 1, "value": C.C2H_DEFEATURE_RSVD}


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
    if t.i != _M3_OPS:
        raise SystemExit(f"  FAIL M3 consumed {t.i} ops, expected {_M3_OPS}")
    return frontier


def _walk_probe(ops) -> tuple[int, object, bytes]:
    """M1 + M2 wire + parses + M3 (probe's hidden-report power) from op 0."""
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
    assert power.power_on(t) is True
    print(f"  PASS M3 power_on #1 ({t.i} ops so far)")
    return t.i, params, table


def run(capture: str | None = None, verbose: bool = False) -> int:
    pcap = _resolve(capture)
    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    print(f"rtl8188ftv_dkms: {len(ops)} ops from {pcap.name} (dev {dev})")
    try:
        if pcap.name == "capture-1.pcap":
            frontier = _walk_m3(ops, 0)
            want = {"kind": "R", "addr": 0x09, "width": 1}
        else:
            frontier, _params, _table = _walk_probe(ops)
            want = _M2_FRONTIER
        op = ops[frontier]
        if any(op.get(k) != v for k, v in want.items()):
            print(f"  FAIL frontier: op#{frontier} is {rp.ReplayTransport._fmt(op)}")
            return 1
        print(f"  PASS frontier: op#{frontier} opens the next milestone "
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
