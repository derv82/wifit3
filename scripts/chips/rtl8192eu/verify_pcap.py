"""Acceptance gate: replay-diff the rtl8192eu (RTL8192EU, TL-WN823N v2/v3) bring-up
against its cold-boot capture.

The 8192e is rtl8xxxu (not rtw88), but the USB register wire format is identical to the
rtw88-family Realtek convention (vendor bRequest 0x05, address in wValue, 0x40/0xC0
write/read split), so it reuses the shared Realtek replay engine.

This capture set is the **mainline rtl8xxxu** cold boot of a TP-Link TL-WN823N v2/v3
(``airmon-ng.log``/``sysinfo.log``: driver rtl8xxxu, ``2357:0109``, kernel 6.19.14), the
same driver this port mirrors (v6.19), so the register *sequence* replays byte-for-byte.

Gates mirror the sibling ``chips/rtl8188eus/verify_pcap.py``, re-anchored for the 8192eu
bring-up order (``core.c`` ``rtl8xxxu_init_device``). Every anchor the gates key off exists
in this capture, so a missing anchor is a FAILURE, never a silent skip.

* **FW blob** -- the rtl8192eu_nic.bin payload as it lands on REG_FW_START_ADDRESS
  (0x1000), in 128-byte chunks, concatenated == the bundled blob.
* **MAC + PHY** -- ``apply_mac_init_table`` (101-entry MAC table) then
  ``post_mac_init_phy`` (BB + AGC + crystal cap + both RF paths), anchored at the MAC
  table's first write (0x0011), driven against the recorded chip reads so every emitted
  write must match the wire.
* **Queue pre-FW** -- ``pre_fw_queue_init`` (queue pages + DMA lanes + RX page
  boundary), anchored at the first write of ``REG_RQPN_NPQ`` (0x0214), before the FW
  upload.
* **Post-PHY block** -- ``post_phy_mac_init`` (TX buffer boundary + PBP + auto-LLT +
  USB quirks), anchored at the first write of ``REG_TXPKTBUF_BCNQ_BDNY`` (0x0424).
* **Beacon block** -- ``init_beacon_params``, anchored at the first 16-bit write of
  ``REG_BEACON_CTRL`` (0x0550).
* **LCK** -- ``phy_lc_calibrate`` (LC tank cal, both paths), anchored at the LC block.
* **IQK** -- ``phy_iq_calibrate`` (2T2R: path A + path B), anchored at the first read of
  REG_FPGA0_XCD_SWITCH_CTRL after the FW region.

The RX-enable + adaptive-controls run (``enable_rx_data_path`` then
``init_adaptive_controls``) is intentionally NOT gated: the port's ``enable_rx_data_path``
writes its monitor-superset ``REG_RCR`` (0x7000780f), while the kernel writes the
station-mode value (0x7000600e) at that point, so a byte-for-byte replay diverges at
``REG_RCR`` -- a deliberate port choice, not a capture gap. The two functions ARE
contiguous on the wire (``init_adaptive_controls`` alone replays clean).

Run: uv run python scripts/chips/rtl8192eu/verify_pcap.py [capture-1|capture-2]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8192eu import firmware, iqk, mac, phy  # noqa: E402
from wifit3.chips.rtl8192eu.constants import (  # noqa: E402
    FW_HEADER_SIZE,
    REG_BEACON_CTRL,
    REG_FPGA0_XCD_SWITCH_CTRL,
    REG_FW_START_ADDRESS,
    REG_OFDM1_LSTF,
    REG_RQPN_NPQ,
    REG_TXPKTBUF_BCNQ_BDNY,
)
from wifit3.chips.rtl8192eu.efuse import EfuseDefaults  # noqa: E402
from wifit3.chips.rtl8192eu.phy import REG_MAC_PHY_CTRL, XTAL0, XTAL0_SHIFT  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rtl8xxxu"
_WHOLE = (1, 10 ** 9)
_FW_REGION_END = REG_FW_START_ADDRESS + 0x1000   # chunks restart at 0x1000 each page
_MAC_INIT_FIRST_REG = 0x0011                      # rtl8192e_mac_init_table[0] (8192e.c:20)
# IQK opener: iqcalibrate reads xa_agc, xb_agc, then adda_regs[0] before saving anything
# (8192e.c:1057-1090). The 0x085c read alone is not unique; the triple is.
_IQK_XA_AGC = 0x0c50                              # REG_OFDM0_XA_AGC_CORE1 (regs.h:1084)
_IQK_XB_AGC = 0x0c58                              # REG_OFDM0_XB_AGC_CORE1 (regs.h:1086)


def _read_index_after(ops, start, addr):
    """First index >= ``start`` of a control read to ``addr``."""
    return next((i for i in range(start, len(ops))
                 if ops[i]["kind"] == "R" and ops[i].get("addr") == addr), None)


def _iqk_start(ops, fw_last):
    """Index of the IQK opener: R 0x0c50, R 0x0c58, R 0x085c in sequence."""
    for i in range(fw_last, len(ops) - 2):
        if (ops[i]["kind"] == "R" and ops[i].get("addr") == _IQK_XA_AGC
                and ops[i + 1]["kind"] == "R" and ops[i + 1].get("addr") == _IQK_XB_AGC
                and ops[i + 2]["kind"] == "R"
                and ops[i + 2].get("addr") == REG_FPGA0_XCD_SWITCH_CTRL):
            return i
    return None


def _blob_gate(ops) -> bool:
    """The uploaded firmware (every write into [0x1000, 0x2000), in order) == the bundled blob."""
    payload = firmware.load_firmware_blob()[FW_HEADER_SIZE:]
    chunks = [o for o in ops
              if o["kind"] == "W" and REG_FW_START_ADDRESS <= o["addr"] < _FW_REGION_END]
    if not chunks:
        print(f"  FAIL: no firmware writes into "
              f"[0x{REG_FW_START_ADDRESS:04x}, 0x{_FW_REGION_END:04x}) in capture")
        return False
    uploaded = b"".join(o["value"].to_bytes(o["width"], "little") for o in chunks)
    print(f"  FW blob: payload {len(payload)}B vs captured upload {len(uploaded)}B "
          f"over {len(chunks)} chunks")
    if uploaded != payload:
        n = min(len(uploaded), len(payload))
        j = next((k for k in range(n) if uploaded[k] != payload[k]), n)
        print(f"  FAIL: uploaded firmware differs from rtl8192eu_nic.bin at byte {j} "
              f"(upload {len(uploaded)}B vs blob {len(payload)}B)")
        return False
    print("  PASS: firmware upload byte-for-byte == bundled rtl8192eu_nic.bin")
    return True


def _report(miles: list[tuple[str, int]]) -> None:
    prev = 0
    for label, end in miles:
        print(f"      {label:30} {end - prev:5} ops")
        prev = end


def _bringup_gate(ops) -> bool:
    """Replay ``apply_mac_init_table`` + ``post_mac_init_phy`` against the capture, anchored at
    the MAC table's first write (0x0011) after the FW upload region. The driver's bring-up
    functions run unchanged against a ``ReplayTransport`` that serves the recorded chip reads,
    so every write they emit must equal the wire or a ``Divergence`` is raised at the first
    mismatch -- a byte-perfect unit test of the port against this capture.

    ``set_crystal_cap`` needs the EFUSE crystal_cap; until the EFUSE *read* is its own gated
    block, we source it from the capture's REG_MAC_PHY_CTRL write (this still verifies that
    set_crystal_cap's read-modify-write reproduces the captured 32-bit value).
    """
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  FAIL: no FW upload region in capture (bring-up anchor unavailable)")
        return False
    anchor = next((i for i in range(fw_writes[-1], len(ops))
                   if ops[i]["kind"] == "W" and ops[i].get("addr") == _MAC_INIT_FIRST_REG), None)
    if anchor is None:
        print(f"  FAIL: MAC init table anchor (0x{_MAC_INIT_FIRST_REG:04x}) not found post-FW")
        return False

    xw = next((o for o in ops[anchor:]
               if o["kind"] == "W" and o.get("addr") == REG_MAC_PHY_CTRL), None)
    crystal_cap = ((xw["value"] & XTAL0) >> XTAL0_SHIFT) if xw else 0
    efuse = EfuseDefaults(default_crystal_cap=crystal_cap)

    rt = rp.ReplayTransport(ops[anchor:])
    miles: list[tuple[str, int]] = []
    try:
        mac.apply_mac_init_table(rt)
        miles.append(("apply_mac_init_table", rt.i))
        phy.post_mac_init_phy(rt, efuse)
        miles.append(("post_mac_init_phy (BB+AGC+xtal+RF A/B)", rt.i))
    except rp.Divergence as e:
        last = miles[-1][0] if miles else "(none)"
        print(f"  FAIL (bring-up divergence after {last}):\n    {e}")
        _report(miles)
        return False

    print(f"  PASS: {rt.i} ops byte-for-byte -- apply_mac_init_table + post_mac_init_phy "
          f"(crystal_cap=0x{crystal_cap:02x})")
    _report(miles)
    return True


def _queue_gate(ops) -> bool:
    """Replay ``pre_fw_queue_init`` against the pre-FW queue block, anchored at the first
    write of ``REG_RQPN_NPQ`` (0x0214). The kernel partitions the queue pages
    (`core.c:3952`), routes the DMA lanes (`core.c:3954`) and sets the RX page boundary
    (`core.c:3962`) before the FW upload (`core.c:3965`). The TL-WN823N reports three
    bulk-OUT endpoints and leaves ``REG_NORMAL_SIE_EP_TX`` at 0, so the queue flags are
    (high, normal, low)."""
    anchor = next((i for i, o in enumerate(ops)
                   if o["kind"] == "W" and o.get("addr") == REG_RQPN_NPQ), None)
    if anchor is None:
        print("  FAIL: no REG_RQPN_NPQ write in capture (queue-pre-FW anchor)")
        return False
    rt = rp.ReplayTransport(ops[anchor:])
    try:
        mac.pre_fw_queue_init(rt, mac.TxQueues(high=True, normal=True, low=True))
    except rp.Divergence as e:
        print(f"  FAIL (queue-pre-FW divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- pre_fw_queue_init "
          f"(queue pages + DMA lanes + RX page boundary)")
    return True


def _post_phy_gate(ops) -> bool:
    """Replay ``post_phy_mac_init`` (TX buffer boundary + PBP + auto-LLT + USB quirks,
    ``core.c:4021-4054``), anchored at the first write of ``REG_TXPKTBUF_BCNQ_BDNY``
    (0x0424) after the PHY/RF block."""
    anchor = next((i for i, o in enumerate(ops)
                   if o["kind"] == "W" and o.get("addr") == REG_TXPKTBUF_BCNQ_BDNY), None)
    if anchor is None:
        print("  FAIL: no REG_TXPKTBUF_BCNQ_BDNY write in capture (post-PHY anchor)")
        return False
    rt = rp.ReplayTransport(ops[anchor:])
    try:
        mac.post_phy_mac_init(rt)
    except rp.Divergence as e:
        print(f"  FAIL (post-PHY block divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- post_phy_mac_init "
          f"(TX boundary + PBP + auto-LLT + USB quirks)")
    return True


def _beacon_gate(ops) -> bool:
    """Replay ``init_beacon_params`` (``core.c:4199-4211``), anchored at the first 16-bit
    write of ``REG_BEACON_CTRL`` (0x0550): the MAC init table writes the same register
    byte-wide, so only the beacon block's ``val16`` write matches."""
    anchor = next((i for i, o in enumerate(ops)
                   if o["kind"] == "W" and o["width"] == 2
                   and o.get("addr") == REG_BEACON_CTRL), None)
    if anchor is None:
        print("  FAIL: no 16-bit REG_BEACON_CTRL write in capture (beacon anchor)")
        return False
    rt = rp.ReplayTransport(ops[anchor:])
    try:
        mac.init_beacon_params(rt)
    except rp.Divergence as e:
        print(f"  FAIL (beacon block divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- init_beacon_params "
          f"(BEACON_CTRL + TBTT_PROHIBIT + early-int + DMA time + TCFG)")
    return True


def _lck_gate(ops) -> bool:
    """Replay ``phy_lc_calibrate`` against the LC-cal block, anchored at the last read of
    REG_OFDM1_LSTF (0x0d00) before the IQK block. The kernel runs LCK immediately before IQK
    (init_device:4290)."""
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  FAIL: no FW region in capture (LCK anchor unavailable)")
        return False
    iqk_anchor = _iqk_start(ops, fw_writes[-1])
    lck_anchor = (max((i for i in range(fw_writes[-1], iqk_anchor)
                       if ops[i]["kind"] == "R" and ops[i].get("addr") == REG_OFDM1_LSTF), default=None)
                  if iqk_anchor is not None else None)
    if lck_anchor is None:
        print("  FAIL: REG_OFDM1_LSTF anchor not in this capture")
        return False
    rt = rp.ReplayTransport(ops[lck_anchor:iqk_anchor])
    try:
        iqk.phy_lc_calibrate(rt)
    except rp.Divergence as e:
        print(f"  FAIL (LCK divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- phy_lc_calibrate (LC tank cal, paths A+B)")
    return True


def _iqk_gate(ops) -> bool:
    """Replay ``phy_iq_calibrate`` against the capture's IQK block, anchored at the first
    *read* of REG_FPGA0_XCD_SWITCH_CTRL (0x085c) after the FW upload -- the ADDA backup that
    opens IQK (and the only read of that reg in the boot). The full 2T2R calibration, the
    similarity-compare candidate pick, ``fill_iqk_matrix_a``/``_b``, and the recovery
    snapshot must replay byte-for-byte."""
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  FAIL: no FW region in capture (IQK anchor unavailable)")
        return False
    anchor = _iqk_start(ops, fw_writes[-1])
    if anchor is None:
        print("  FAIL: ADDA-save anchor (read 0x085c) not in this capture")
        return False
    rt = rp.ReplayTransport(ops[anchor:])
    try:
        iqk.phy_iq_calibrate(rt)
    except rp.Divergence as e:
        print(f"  FAIL (IQK divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- phy_iq_calibrate "
          f"(2T2R IQK + similarity + fill_matrix A/B + recovery snapshot)")
    return True


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev, _WHOLE)
    print(f"{name}: card=dev{dev}, {len(ops)} driver-side ops")
    rp.audit_coverage(pcap, dev)

    ok = _blob_gate(ops)
    ok = _bringup_gate(ops) and ok
    ok = _queue_gate(ops) and ok
    ok = _post_phy_gate(ops) and ok
    ok = _beacon_gate(ops) and ok
    ok = _lck_gate(ops) and ok
    ok = _iqk_gate(ops) and ok

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
