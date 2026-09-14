"""Acceptance gate: replay-diff the rtl8188ftv (RTL8188FTV, 0bda:f179) bring-up
against its cold-boot capture.

The 8188F is rtl8xxxu (mainline), USB vendor-control wire 0x05.  The capture
(``driver_captures/captures_rtl8188ftv/capture-1.pcap``) was taken with kernel
6.12.107, driver ``rtl8xxxu.ko.xz``.

Milestones gated here:

* **FW blob** -- the rtl8188fufw.bin payload as it lands on
  ``REG_FW_START_ADDRESS`` (0x1000) in 128-byte chunks, laddered across
  4096-byte pages.  Concatenated upload bytes == the bundled blob.
* **MAC + PHY** -- ``init_mac`` (MAC table; MAX_AGGR is 8188f's
  ``default: break``) then ``post_mac_init_phy`` (BB + AGC tables +
  ``set_crystal_cap`` + RF path-A with the RFENV/INT_OE/HSSI preamble),
  then ``init_device_post_phy`` (RFSW, TX boundary, LLT, USB quirks,
  RCR/SIFS/EDCA, burst, aggregation, statistics), anchored at the MAC
  table's first write (0x0024), driven against the recorded chip reads so
  every emitted write must match the wire.
* **LC + IQ + RF tail** -- ``lc_calibrate`` (LSTF/TXPAUSE branch +
  RF MODE_AG poll), ``iq_calibrate`` (phy path A inner/outer, simularity
  compare, IQ matrix), ``enable_thermal_meter`` (RF 0x42 RMW),
  ``init_device_phy_tail`` (NAV_UPPER + FWHW_TXQ ack + CCK/CFO reads) and
  ``enable_rf`` (EFUSE BB-gain trim, RF_CTRL, path A), driven against
  the recorded chip reads so every emitted write must match the wire.
* **RX path + channel 1 tune** -- ``enable_rx_path`` (filt maps + AGC
  IGI 0x1e), monitor ``configure_filter`` x3, ``set_tx_power(1)`` and
  ``set_channel_2g_20mhz(1)`` (spur calibration, 20 MHz BB, RF
  TRX_BW/filters), driven against the recorded chip reads so every
  emitted write must match the wire (ops 2295-2347).

Run: uv run python scripts/chips/rtl8188ftv/verify_pcap.py [capture-1]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv import chan, efuse, firmware, mac, phy
from wifit3.chips.rtl8188ftv.constants import (
    FW_HEADER_SIZE,
    REG_AFE_XTAL_CTRL,
    REG_EFUSE_ACCESS,
    REG_FW_START_ADDRESS,
    REG_SYS_CFG,
    RTL_FW_PAGE_SIZE,
)

CAP_DIR = REPO / "driver_captures" / "captures_rtl8188ftv"
_WHOLE = (1, 10 ** 9)
_FW_REGION_END = REG_FW_START_ADDRESS + RTL_FW_PAGE_SIZE  # 0x2000
_MAC_INIT_FIRST_REG = 0x0024    # rtl8188f_mac_init_table[0] (8188f.c:18)


def _efuse_gate(ops) -> dict | None:
    """Replay ``read_efuse_map`` + ``parse_efuse_8188fu`` against the capture's
    EFUSE region (REG_9346CR read, ACCESS_ENABLE, per-byte polls, ACCESS_DISABLE)
    from the first REG_EFUSE_ACCESS write to end of ops.  Returns the parsed
    EfuseDefaults (used for the MAC+PHY gate's crystal_cap).
    """
    # EFUSE region: first ACCESS write (0xcf) to the last op before the FW
    # upload starts.  The upload is anchored at REG_FW_START_ADDRESS writes.
    first_access = next((i for i, o in enumerate(ops)
                         if o["kind"] == "W" and o.get("addr") == REG_EFUSE_ACCESS), None)
    if first_access is None:
        print("  efuse: no REG_EFUSE_ACCESS write in capture -- skipped")
        return None
    # read_and_parse starts with read16(REG_9346CR), which is one op before
    # the first ACCESS_ENABLE write (or at first_access if the 9346CR read
    # was absorbed into an earlier gate).  Anchor back to include it.
    anchor = max(first_access - 1, 0)
    fw_start = next((i for i, o in enumerate(ops)
                     if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS), None)
    end = fw_start if fw_start is not None else len(ops)
    rt = rp.ReplayTransport(ops[anchor:end])
    try:
        parsed = efuse.read_and_parse(rt)
    except (rp.Divergence, IOError) as e:
        print(f"  FAIL (efuse divergence):\n    {e}")
        raise
    print(f"  PASS: EFUSE read+parse -- {rt.i} ops, "
          f"MAC={parsed.mac_address.hex(':')}, "
          f"crystal_cap=0x{parsed.default_crystal_cap:02x}, "
          f"cck=[{','.join(f'0x{x:02x}' for x in parsed.cck_tx_power_index_A)}], "
          f"ht40_1s=[{','.join(f'0x{x:02x}' for x in parsed.ht40_1s_tx_power_index_A)}]")
    return parsed


def _blob_gate(ops) -> bool:
    """The uploaded firmware (every write into [0x1000, 0x2000), in order) == the
    bundled payload (after stripping the 32-byte header).

    The 8188F writeN(0x1000, page, 4096) produces 32 × 128-byte control
    writes at 0x1000/0x1080/0x1100/.../0x1f80 — a ladder the capture confirms.
    """
    payload = firmware.load_firmware_blob()[FW_HEADER_SIZE:]
    chunks = [o for o in ops
              if o["kind"] == "W"
              and REG_FW_START_ADDRESS <= o["addr"] < _FW_REGION_END
              and o.get("width", 0) > 16]
    uploaded = b"".join(o["value"].to_bytes(o["width"], "little") for o in chunks)
    print(f"  FW blob: payload {len(payload)}B vs captured upload {len(uploaded)}B "
          f"over {len(chunks)} chunks (expect {len(payload) // 128 + bool(len(payload) % 128)} chunks)")
    if uploaded != payload:
        n = min(len(uploaded), len(payload))
        j = next((k for k in range(n) if uploaded[k] != payload[k]), n)
        print(f"  FAIL: uploaded firmware differs from rtl8188fufw.bin at byte {j}")
        print(f"        (upload[0x{j:x}]=0x{uploaded[j]:02x} vs blob[0x{j:x}]=0x{payload[j]:02x})")
        return False
    print("  PASS: firmware upload byte-for-byte == bundled rtl8188fufw.bin")
    return True


def _report(miles: list[tuple[str, int]]) -> None:
    prev = 0
    for label, end in miles:
        print(f"      {label:30} {end - prev:5} ops")
        prev = end


def _bringup_gate(ops, efuse_defaults: dict | None = None) -> bool:
    """Replay ``init_mac`` + ``post_mac_init_phy`` + ``init_device_post_phy`` + LC/IQ/RF
    tail against the capture, anchored at the MAC init table (first write to 0x0024 after
    the FW upload region). The driver's bring-up functions run unchanged against a
    ``ReplayTransport`` that serves the recorded chip reads, so every write they emit must
    equal the wire or a ``Divergence`` is raised at the first mismatch.

    ``set_crystal_cap`` needs the EFUSE crystal_cap and ``init_phy_rf`` the chip_cut.
    ``efuse_defaults`` is the EFUSE gate's parsed result -- its ``default_crystal_cap`` now
    drives the PHY gate.  chip_cut is derived from the ``read32(REG_SYS_CFG)`` in
    identify_chip.  ``time.sleep`` is patched (the expired clock fake) because the replay
    must never stall on ``usleep_range``.
    """
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  bring-up: no FW upload region in capture -- skipped")
        return True
    anchor = next((i for i in range(fw_writes[-1], len(ops))
                   if ops[i]["kind"] == "W" and ops[i].get("addr") == _MAC_INIT_FIRST_REG), None)
    if anchor is None:
        print(f"  FAIL: MAC init table anchor (0x{_MAC_INIT_FIRST_REG:04x}) not found post-FW")
        return False

    sys_cfg_read = next((o for o in ops
                         if o["kind"] == "R" and o.get("addr") == REG_SYS_CFG), None)
    if sys_cfg_read is None:
        print("  FAIL: no REG_SYS_CFG read to derive chip_cut")
        return False
    chip_cut = (sys_cfg_read["value"] >> 12) & 0xF

    if efuse_defaults is not None:
        crystal_cap = efuse_defaults.default_crystal_cap
        eff = f"efuse (0x{crystal_cap:02x})"
    else:
        xw = next((o for o in ops[anchor:]
                   if o["kind"] == "W" and o.get("addr") == REG_AFE_XTAL_CTRL
                   and o.get("width") == 4), None)
        crystal_cap = ((xw["value"] >> 11) & 0x3F) if xw else 0
        eff = f"wire ({crystal_cap:02x})"

    rt = rp.ReplayTransport(ops[anchor:])
    miles: list[tuple[str, int]] = []
    try:
        mac.apply_mac_init_table(rt)
        miles.append(("init_mac (MAC table)", rt.i))
        phy.post_mac_init_phy(rt, chip_cut, crystal_cap)
        miles.append(("post_mac_init_phy (BB+AGC+xtal+RF)", rt.i))
        if efuse_defaults is not None:
            mac.init_device_post_phy(rt, efuse_defaults)
            miles.append(("init_device_post_phy (RFSW..CCK PD)", rt.i))
        phy.lc_calibrate(rt)
        miles.append(("lc_calibrate", rt.i))
        phy.iq_calibrate(rt)
        miles.append(("iq_calibrate", rt.i))
        phy.enable_thermal_meter(rt)
        miles.append(("enable_thermal_meter", rt.i))
        phy.init_device_phy_tail(rt)
        miles.append(("init_device_phy_tail", rt.i))
        phy.enable_rf(rt)
        miles.append(("enable_rf", rt.i))
        mac.enable_rx_path(rt)
        miles.append(("enable_rx_path (start tail)", rt.i))
        for _ in range(3):
            mac.configure_filter(rt)
        miles.append(("configure_filter x3 (monitor RCR)", rt.i))
        if efuse_defaults is not None:
            phy.set_tx_power(rt, 1, efuse_defaults)
        miles.append(("set_tx_power(1)", rt.i))
        chan.set_channel_2g_20mhz(rt, 1)
        miles.append(("set_channel_2g_20mhz(1)", rt.i))
    except rp.Divergence as e:
        last = miles[-1][0] if miles else "(none)"
        print(f"  FAIL (bring-up divergence after {last}):\n    {e}")
        _report(miles)
        return False

    print(f"  PASS: {rt.i} ops byte-for-byte -- init_mac + post_mac_init_phy + "
          f"LC/IQ/RF tail + RX path/channel-1 tune "
          f"(chip_cut={chip_cut}, crystal_cap=0x{crystal_cap:02x} from {eff})")
    _report(miles)
    return True


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev, _WHOLE)
    print(f"{name}: card=dev{dev}, {len(ops)} driver-side ops")

    ok = True
    efuse_defaults = None
    try:
        efuse_defaults = _efuse_gate(ops)
    except (rp.Divergence, IOError, AssertionError, ValueError):
        ok = False
    ok = efuse_defaults is not None and ok
    ok = _blob_gate(ops) and ok
    ok = _bringup_gate(ops, efuse_defaults) and ok

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())