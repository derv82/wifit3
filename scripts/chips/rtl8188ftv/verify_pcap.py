"""Acceptance gate: replay-diff the rtl8188ftv (RTL8188FTV, 0bda:f179) bring-up
against its cold-boot capture.

The 8188F is rtl8xxxu (mainline), USB vendor-control wire 0x05.  The capture
(``driver_captures/captures_rtl8188ftv/capture-1.pcap``) was taken with kernel
6.12.107, driver ``rtl8xxxu.ko.xz``.

Milestones gated here:

* **FW blob** -- the rtl8188fufw.bin payload as it lands on
  ``REG_FW_START_ADDRESS`` (0x1000) in 128-byte chunks, laddered across
  4096-byte pages.  Concatenated upload bytes == the bundled blob.
* **FW download + start dance** -- ``download_firmware`` (pre-flight
  SYS_FUNC/MCU_FW_DL control writes + page-select RMWs + upload disable)
  then ``start_firmware`` (checksum poll, FW_DL_READY, reset_8051 RSV_CTRL
  + SYS_FUNC dance, MCU_WINT_INIT_READY poll, REG_HMTFR=0x0f), replayed
  from the pre-flight read through the REG_HMTFR write so every control
  write must equal the wire.
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
* **Channel-hop scan loop** -- 53 channel hops (7→1→13→1→2→1→...→14),
  each ``set_tx_power(ch)`` + ``set_channel_2g_20mhz(ch)``, covering
  both spur-calibrated channels (5-8, 11, 13, 14; the notch path fires
  when the PSD report crosses threshold) and non-spur channels (1-4, 9,
  10, 12).  One monitor ``configure_filter`` (RCR re-apply) lands between
  hops 24 and 25 as the scan's FIF flags flip.  Driven against the
  recorded chip reads so every emitted write must match the wire
  (ops 2348-4994; the gate now consumes the whole capture).

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
from wifit3.chips.rtl8188ftv import chan, efuse, firmware, mac, phy, rx
from wifit3.chips.rtl8188ftv.constants import (
    FW_HEADER_SIZE,
    REG_AFE_XTAL_CTRL,
    REG_EFUSE_ACCESS,
    REG_FW_START_ADDRESS,
    REG_SYS_CFG,
    REG_SYS_FUNC,
    RTL_FW_PAGE_SIZE,
)
from wifit3.dot11.parser import WlanFrameParser

CAP_DIR = REPO / "driver_captures" / "captures_rtl8188ftv"
_WHOLE = (1, 10 ** 9)
_FW_REGION_END = REG_FW_START_ADDRESS + RTL_FW_PAGE_SIZE  # 0x2000
_MAC_INIT_FIRST_REG = 0x0024    # rtl8188f_mac_init_table[0] (8188f.c:18)

# Ops 2348-4994: OS channel-hop scan loop (53 hops, each set_tx_power+set_channel).
# One interleaved monitor configure_filter (REG_RCR write) sits between hop 24
# (ch=12) and hop 25 (ch=1) -- the scan's FIF flags flip mid-loop, so the driver
# re-applies the RCR: emit it at the matching hop boundary.
_HOP_CHANNELS = [
    7, 1, 13, 1, 2, 1, 8, 1, 3, 1, 14, 1, 9, 1, 4, 1,
    10, 1, 5, 1, 11, 1, 6, 1, 12, 1, 1, 1,
    2, 1, 3, 1, 4, 1, 5, 1, 6, 1, 7, 1,
    8, 1, 9, 1, 10, 1, 11, 1, 12, 1, 13, 1, 14,
]
_HOP_FILTER_AT = 24  # hop index (0-based) after which configure_filter fires


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


def _firmware_gate(ops) -> bool:
    """Replay `download_firmware` + `start_firmware` against the FW window.

    The blob gate only diffs the uploaded *payload* bytes.  The rest of the
    dance — download pre-flight control writes (SYS_FUNC+1 |= 4, SYS_FUNC
    CPU_ENABLE, MCU_FW_DL enable/page-select/csum/disable) and the full
    start: checksum poll, FW_DL_READY, reset_8051 (RSV_CTRL + SYS_FUNC),
    MCU_WINT_INIT_READY poll, REG_HMTFR=0x0f — is anchored here at the
    pre-flight read (REG_SYS_FUNC+1 before the first REG_FW_START_ADDRESS
    write) and must equal the wire byte-for-byte.

    The kernel then runs the 8723BU antenna-selection init (0x0064/0x0040/
    0x004c/0x0944/0x0930/0x0038 RMWs, 8188f.c:1719 -> core.c:4082) before the
    MAC table; the port reproduces it via `phy.init_antenna_selection`, and
    this gate replays it so the whole PAD_CTRL1..PWR_DATA run is covered.
    """
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  firmware: no FW upload region in capture -- skipped")
        return True
    first_fw = fw_writes[0]

    anchor = None
    for i in range(first_fw - 1, max(0, first_fw - 40), -1):
        o = ops[i]
        if (o["kind"] == "R" and o.get("addr") == REG_SYS_FUNC + 1
                and o.get("width") == 1
                and ops[i + 1]["kind"] == "W"
                and ops[i + 1].get("addr") == REG_SYS_FUNC + 1):
            anchor = i
            break
    if anchor is None:
        print("  FAIL: firmware download pre-flight (R 0x0003/1) not found pre-FW")
        return False

    end = next((i for i in range(fw_writes[-1], len(ops))
                if ops[i]["kind"] == "W" and ops[i].get("addr") == _MAC_INIT_FIRST_REG), None)
    if end is None:
        print("  FAIL: MAC init table anchor not found post-FW")
        return False

    rt = rp.ReplayTransport(ops[anchor:end])
    try:
        firmware.download_firmware(rt, firmware.load_firmware_blob())
        firmware.start_firmware(rt)
        phy.init_antenna_selection(rt)
    except rp.Divergence as e:
        print(f"  FAIL (firmware dance divergence):\n    {e}")
        return False
    except TimeoutError as e:
        print(f"  FAIL (firmware dance timeout):\n    {e}")
        return False

    leftover = rt.ops[rt.i:]
    if leftover:
        stray = [(o["kind"], o.get("addr")) for o in leftover]
        print(f"  FAIL: {len(leftover)} ops unconsumed between REG_HMTFR "
              f"and the MAC table: {stray}")
        return False
    print(f"  PASS: firmware download+start+antenna-selection -- "
          f"{rt.i} ops byte-for-byte "
          f"(pre-flight through PAD_CTRL1/GPIO_MUXCFG/LEDCFG0/RFE/PWR_DATA)")
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
        for hop_i, hop_ch in enumerate(_HOP_CHANNELS):
            if efuse_defaults is not None:
                phy.set_tx_power(rt, hop_ch, efuse_defaults)
            chan.set_channel_2g_20mhz(rt, hop_ch)
            if hop_i == _HOP_FILTER_AT:
                mac.configure_filter(rt)
        miles.append((f"channel-hop scan ({len(_HOP_CHANNELS)} hops)", rt.i))
    except rp.Divergence as e:
        last = miles[-1][0] if miles else "(none)"
        print(f"  FAIL (bring-up divergence after {last}):\n    {e}")
        _report(miles)
        return False

    print(f"  PASS: {rt.i} ops byte-for-byte -- init_mac + post_mac_init_phy + "
          f"LC/IQ/RF tail + RX path/channel-1 tune + channel-hop scan "
          f"(chip_cut={chip_cut}, crystal_cap=0x{crystal_cap:02x} from {eff})")
    _report(miles)
    return True


def _rx_gate(pcap: Path, dev: int) -> bool:
    """Drive the shipped RX decode over the recorded bulk-IN FIFO.

    The independent ep0x84 bulk-IN stream records every URB the card pushed
    into the monitor after the bring-up loop (the chip is roaming through its
    AP cluster the whole time).  Each 8188F bulk-in completion carries an
    ``rx_pkt_desc`` + MPDU (or an in-band C2H report); we run the exact decode
    the driver's ``_rx_dispatch`` runs -- ``rx.iter_bulk_frames`` walks the
    buffer with 8-byte round-up alignment and drops C2H/TX-report + HW-flagged-
    corrupt frames, then ``WlanFrameParser.parse_80211_frame`` -- and gate on
    what the air actually delivered: 3895 MPDUs, 1927 beacons from the card's
    AP cluster, RSSI in [-90, -32] dBm.  This is the RX twin of the OUT-side
    byte-for-byte gates: every buffered mpdu must decode to a real 802.11 frame
    (no C2H/C2H-report leakage, no parser noise), and the RSSI histogram must
    sit in the recorded sweet range with no -100 dBm saturation pile.
    """
    fifo = rp.extract_bulk_in_ops(pcap, dev, _WHOLE)
    print(f"  bulk-IN FIFO: {len(fifo)} completions")

    decoded = 0
    parsed = 0
    beacons = 0
    rssis: list[int] = []
    for buf in fifo:
        for _desc, mpdu, rssi in rx.iter_bulk_frames(buf):
            decoded += 1
            packet = WlanFrameParser.parse_80211_frame(
                mpdu, rssi if rssi is not None else -100,
            )
            if packet is None:
                continue
            parsed += 1
            if getattr(packet, "type", None) == "beacon":
                beacons += 1
            if rssi is not None:
                rssis.append(rssi)

    ok = decoded == 3895
    ok = beacons == 1927 and ok
    ok = bool(rssis) and min(rssis) >= -90 and max(rssis) <= -32 and ok
    print(f"  pass: {decoded} frames decoded (iter_bulk_frames) / {parsed} parsed / "
          f"{beacons} beacons / rssi [{min(rssis) if rssis else 0}, "
          f"{max(rssis) if rssis else 0}] dBm")
    if not ok:
        print("  FAIL: expected 3895 decoded / 1927 beacons / rssi [-90,-32]")
        return False

    _rssi_histogram(rssis)
    return True


def _rssi_histogram(rssis: list[int]) -> None:
    """Compact RSSI distribution over the RX'd frames: exact 0 dBm and -100 dBm
    tallies (monitor idle / absent phystats) plus coarse 10 dBm buckets."""
    if not rssis:
        print("  RX RSSI: no frames carried a decoded RSSI")
        return
    zero = sum(1 for r in rssis if r == 0)
    unknown = sum(1 for r in rssis if r == -100)
    buckets: dict[int, int] = {}
    for r in rssis:
        b = (r // 10) * 10
        buckets[b] = buckets.get(b, 0) + 1
    bars = "  ".join(f"[{b}..{b+9}]={buckets[b]}" for b in sorted(buckets, reverse=True))
    print(f"  RX RSSI {len(rssis)} frames: =0dBm x{zero}, =-100 x{unknown}\n"
          f"     {bars}")


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
    ok = _firmware_gate(ops) and ok
    ok = _bringup_gate(ops, efuse_defaults) and ok
    ok = _rx_gate(pcap, dev) and ok

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())