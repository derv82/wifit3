"""Acceptance gate: replay-diff the rtl8188eus (RTL8188EUS, TL-WN722N v2/v3) bring-up
against its cold-boot capture.

The 8188e is rtl8xxxu, not rtw88, but the USB register wire format is identical (Realtek
vendor 0x05, address in wValue), so it reuses the shared Realtek replay engine.

This capture set is the **mainline rtl8xxxu** cold boot (``airmon-ng.log``: driver rtl8xxxu) --
the driver this port mirrors -- so the register *sequence* replays byte-for-byte, not just the
blob. (The DKMS/vendor ``realtek-rtl8188eus`` boot in ``usb_dumps_new/captures_8188eu/`` is the
target of a separate vendor port.) Coverage grows milestone-by-milestone as the port is walked
against the kernel source:

* **FW blob** -- the rtl8188eufw.bin payload as it lands on REG_FW_START_ADDRESS (0x1000), in
  196-byte chunks, concatenated == the bundled blob.
* **MAC + PHY** -- ``init_mac`` (MAC table + MAX_AGGR) then ``post_mac_init_phy``
  (BB + AGC tables + ``set_crystal_cap`` + RF path A), anchored at the MAC table's first write
  (0x0026), driven against the recorded chip reads so every emitted write must match the wire.

Not yet gated (driver still diverges from the wire here): the EFUSE *read* (crystal_cap is
sourced from the capture's own REG_AFE_XTAL_CTRL write below), the pre-FW power-on /
queue-init order, and the post-PHY block (RFSW control, TX-buffer/LLT, usb_quirks, RCR,
adaptive controls, IQK/LCK). Those land as the bring-up is restructured to mirror init_device.

Run: uv run python scripts/rtl8188eus/verify_pcap.py [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus import firmware, iqk, mac, phy  # noqa: E402
from wifit3.chips.rtl8188eus.constants import (  # noqa: E402
    FW_HEADER_SIZE,
    REG_AFE_XTAL_CTRL,
    REG_FPGA0_XCD_SWITCH_CTRL,
    REG_FW_START_ADDRESS,
    REG_OFDM1_LSTF,
    XTAL0_SHIFT,
)
from wifit3.chips.rtl8188eus.efuse import EfuseDefaults  # noqa: E402

CAP_DIR = REPO / "usb_dumps" / "captures_rtl8xxxu"
_WHOLE = (1, 10 ** 9)
_FW_REGION_END = REG_FW_START_ADDRESS + 0x1000   # chunks restart at 0x1000 each page
_MAC_INIT_FIRST_REG = 0x0026                      # rtl8188e_mac_init_table[0] (8188e.c:19)


def _blob_gate(ops) -> bool:
    """The uploaded firmware (every write into [0x1000, 0x2000), in order) == the bundled blob."""
    payload = firmware.load_firmware_blob()[FW_HEADER_SIZE:]
    chunks = [o for o in ops
              if o["kind"] == "W" and REG_FW_START_ADDRESS <= o["addr"] < _FW_REGION_END]
    uploaded = b"".join(o["value"].to_bytes(o["width"], "little") for o in chunks)
    print(f"  FW blob: payload {len(payload)}B vs captured upload {len(uploaded)}B "
          f"over {len(chunks)} chunks")
    if uploaded != payload:
        n = min(len(uploaded), len(payload))
        j = next((k for k in range(n) if uploaded[k] != payload[k]), n)
        print(f"  FAIL: uploaded firmware differs from rtl8188eufw.bin at byte {j} "
              f"(upload {len(uploaded)}B vs blob {len(payload)}B)")
        return False
    print("  PASS: firmware upload byte-for-byte == bundled rtl8188eufw.bin")
    return True


def _report(miles: list[tuple[str, int]]) -> None:
    prev = 0
    for label, end in miles:
        print(f"      {label:30} {end - prev:5} ops")
        prev = end


def _bringup_gate(ops) -> bool:
    """Replay ``init_mac`` + ``post_mac_init_phy`` against the capture, anchored at the MAC
    init table (first write to 0x0026 after the FW upload region). The driver's bring-up
    functions run unchanged against a ``ReplayTransport`` that serves the recorded chip reads,
    so every write they emit must equal the wire or a ``Divergence`` is raised at the first
    mismatch -- a byte-perfect unit test of the port against this capture.

    ``set_crystal_cap`` needs the EFUSE crystal_cap; until the EFUSE *read* is its own gated
    block, we source it from the capture's REG_AFE_XTAL_CTRL write (this still verifies that
    set_crystal_cap's read-modify-write reproduces the captured 32-bit value).
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

    xw = next((o for o in ops[anchor:]
               if o["kind"] == "W" and o.get("addr") == REG_AFE_XTAL_CTRL), None)
    crystal_cap = ((xw["value"] >> XTAL0_SHIFT) & 0x3F) if xw else 0
    efuse = EfuseDefaults(default_crystal_cap=crystal_cap)

    rt = rp.ReplayTransport(ops[anchor:])
    miles: list[tuple[str, int]] = []
    try:
        mac.apply_mac_init_table(rt)
        miles.append(("init_mac (table + MAX_AGGR)", rt.i))
        phy.post_mac_init_phy(rt, efuse)
        miles.append(("post_mac_init_phy (BB+AGC+xtal+RF)", rt.i))
    except rp.Divergence as e:
        last = miles[-1][0] if miles else "(none)"
        print(f"  FAIL (bring-up divergence after {last}):\n    {e}")
        _report(miles)
        return False

    print(f"  PASS: {rt.i} ops byte-for-byte -- init_mac + post_mac_init_phy "
          f"(crystal_cap=0x{crystal_cap:02x})")
    _report(miles)
    return True


def _lck_gate(ops) -> bool:
    """Replay ``phy_lc_calibrate`` against the LC-cal block, anchored at the last read of
    REG_OFDM1_LSTF (0x0d00) before the IQK block — the kernel runs LCK immediately before
    IQK (init_device:4290). Bounded to end at the IQK anchor, so a full replay confirms LCK
    fills the gap byte-for-byte."""
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  LCK: no FW region -- skipped")
        return True
    iqk_anchor = next((i for i in range(fw_writes[-1], len(ops))
                       if ops[i]["kind"] == "R" and ops[i].get("addr") == REG_FPGA0_XCD_SWITCH_CTRL),
                      None)
    lck_anchor = (max((i for i in range(fw_writes[-1], iqk_anchor)
                       if ops[i]["kind"] == "R" and ops[i].get("addr") == REG_OFDM1_LSTF), default=None)
                  if iqk_anchor is not None else None)
    if lck_anchor is None:
        print("  LCK: REG_OFDM1_LSTF anchor not in this capture -- skipped")
        return True
    rt = rp.ReplayTransport(ops[lck_anchor:iqk_anchor])
    try:
        iqk.phy_lc_calibrate(rt)
    except rp.Divergence as e:
        print(f"  FAIL (LCK divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- phy_lc_calibrate (LC tank cal, path A)")
    return True


def _iqk_gate(ops) -> bool:
    """Replay ``phy_iq_calibrate`` against the capture's IQK block, anchored at the first
    *read* of REG_FPGA0_XCD_SWITCH_CTRL (0x085c) after the FW upload -- the ADDA backup that
    opens IQK (and the only read of that reg in the boot). The full 3-iteration calibration,
    the similarity-compare candidate pick, ``fill_iqk_matrix_a``, and the recovery snapshot
    must replay byte-for-byte: every correction write is computed from the recorded
    measurement-reads the replay serves, so a PASS proves the algorithm is kernel-faithful."""
    fw_writes = [i for i, o in enumerate(ops)
                 if o["kind"] == "W" and o.get("addr") == REG_FW_START_ADDRESS]
    if not fw_writes:
        print("  IQK: no FW region -- skipped")
        return True
    anchor = next((i for i in range(fw_writes[-1], len(ops))
                   if ops[i]["kind"] == "R" and ops[i].get("addr") == REG_FPGA0_XCD_SWITCH_CTRL),
                  None)
    if anchor is None:
        print("  IQK: ADDA-save anchor (read 0x085c) not in this capture -- skipped")
        return True
    rt = rp.ReplayTransport(ops[anchor:])
    try:
        iqk.phy_iq_calibrate(rt)
    except rp.Divergence as e:
        print(f"  FAIL (IQK divergence):\n    {e}")
        return False
    print(f"  PASS: {rt.i} ops byte-for-byte -- phy_iq_calibrate "
          f"(3-iter path-A IQK + similarity + fill_matrix + recovery snapshot)")
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

    ok = _blob_gate(ops)
    ok = _bringup_gate(ops) and ok
    ok = _lck_gate(ops) and ok
    ok = _iqk_gate(ops) and ok

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
