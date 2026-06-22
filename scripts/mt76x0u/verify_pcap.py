"""verify_pcap for MT76x0U (MT7610U) — the faithfulness gate for cold-boot bring-up.

Drives the driver's REAL bring-up (chips/mt76x0u/*) against an mt76-USB cold-boot capture
via the SHARED ReplayDevice (scripts/mt76usb_pcap_replay.py — same 0x06/0x07 codec as
mt76x2u): recorded reads / MCU responses are served back and every register write / bulk-OUT
frame is asserted byte-for-byte. No hardware, no reimplementation.

mt76x0u differs from mt76x2u: register I/O is default-bus only (no CFG/EEPROM vendor buses —
EFUSE is read through the EFUSE_CTRL register over 0x07, so no off-cursor EEPROM map is needed),
the chip is 1T1R, and aireplay TX in the capture is 2.4 GHz only (no 5 GHz inject to verify).

Checks, mirroring the mt7921au / mt76x2u templates:

  CHECK A+B — cold reset + firmware upload (single cursor): FirmwareUploader.load_firmware
              does chip_onoff reset + ILM/DLM bulk upload + IVB trigger + FW_READY poll.
  CHECK C   — post-FW init (continues the cursor): init_usb_dma + waits + reset_csr_bbp +
              Q_SELECT + init_mac_registers + init_bbp + table clears + EFUSE + mac_setaddr +
              phy_init. (mcu_init_smoke_test is a wifit3 diagnostic absent from the kernel
              wire and is omitted.)
  CHECK D   — TX inject (2.4 GHz): every aireplay TX frame on EP 0x07 (AC_VO), rebuilt via
              tx.build_inject_packet and asserted byte-for-byte.

A RED here is a faithful result: a genuine driver-vs-wire divergence is localized and left
red per the gate mandate, never patched away.

Usage: uv run python scripts/mt76x0u/verify_pcap.py [<pcap>]
"""
from __future__ import annotations

import asyncio
import struct
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import mt76usb_pcap_replay as rp  # noqa: E402

from wifit3.chips.mt76x0u import tx as mt_tx  # noqa: E402
from wifit3.chips.mt76x0u.constants import (  # noqa: E402
    MT_WLAN_FUN_CTRL,
    Q_SELECT,
)
from wifit3.chips.mt76x0u.eeprom import read_efuse_full  # noqa: E402
from wifit3.chips.mt76x0u.firmware import FirmwareUploader  # noqa: E402
from wifit3.chips.mt76x0u.mac import (  # noqa: E402
    clear_shared_keys,
    clear_wcids,
    init_mac_registers,
    mac_setaddr,
    wait_for_txrx_idle,
    wait_for_wpdma,
)
from wifit3.chips.mt76x0u.mcu import MCUChannel  # noqa: E402
from wifit3.chips.mt76x0u.phy import init_bbp, phy_init  # noqa: E402
from wifit3.chips.mt76x0u.transport import MT76x0UTransport  # noqa: E402

DEFAULT_CAP = "usb_dumps_new/captures_mt76x0u/capture-1.pcap"
ASSETS = REPO / "src" / "wifit3" / "chips" / "mt76x0u" / "assets"

_TXWI_LEN = 20
_DMA_INFO_LEN = 4
_FC_NAME = {0xc0: "deauth", 0xa0: "disassoc", 0xb0: "auth", 0x40: "probe_req",
            0x50: "probe_resp", 0x48: "null", 0x88: "qos_null", 0x80: "beacon"}


def _patch_sleeps() -> None:
    """Replay needs no real settle delays; the cursor sequences ops, not wall-clock."""
    time.sleep = lambda *a, **k: None

    async def _nosleep(*a, **k):
        return None
    asyncio.sleep = _nosleep


def _fw_file() -> Path:
    primary = ASSETS / "mt7610e_linux-firmware.bin"
    fallback = ASSETS / "mt7610u_linux-firmware.bin"
    return primary if primary.exists() else fallback


# ---------------------------------------------------------------------------
# CHECK A+B+C — one monotonic cursor: cold reset + FW upload + post-FW init.
# ---------------------------------------------------------------------------

def check_boot(data: dict) -> str:
    ops = data["host_ops"]
    anchor = rp.find_anchor(
        ops, lambda o: (o["kind"] == "ctrl" and o["dir"] == "IN" and o["breq"] == 0x07
                        and (o["wval"] << 16 | o["widx"]) == MT_WLAN_FUN_CTRL))
    if anchor is None:
        print("  [FAIL] cold-boot anchor (WLAN_FUN_CTRL read) not found in capture")
        return "fail"

    # wifit3's load_firmware reads WLAN_FUN_CTRL once for a warm-chip check the kernel
    # cold-boot wire never issues; serve that first read off-cursor (WLAN_EN clear ->
    # cold path) so the chip_onoff reset that follows is still verified. The many later
    # reads of the same register (chip_onoff rmw cycles) stay positional.
    dev = rp.ReplayDevice(ops, data["eeprom"], data["responses"], start=anchor,
                          extra_reads={MT_WLAN_FUN_CTRL: 0x00000000})
    t = MT76x0UTransport(dev)
    mcu = MCUChannel(t)
    state = {"phase": "A+B", "marks": {}}

    def _drive():
        # --- CHECK A+B: cold reset + firmware upload -----------------------
        state["phase"] = "A+B"
        uploader = FirmwareUploader(t)
        uploader.load_firmware(_fw_file())
        state["marks"]["AB"] = dev.i

        # --- CHECK C: post-FW init ----------------------------------------
        # mcu_init_smoke_test (CMD_RANDOM_READ) is a wifit3 diagnostic absent from
        # the kernel cold-boot wire; omitted so the cursor stays on the wire.
        state["phase"] = "C"
        uploader.init_usb_dma()
        wait_for_wpdma(t)
        uploader.wait_for_mac()
        uploader.reset_csr_bbp()
        mcu.function_select(Q_SELECT, 1)
        init_mac_registers(t, mcu)
        wait_for_txrx_idle(t)
        init_bbp(t, mcu)
        clear_shared_keys(t)
        clear_wcids(t)
        efuse = read_efuse_full(t)
        mac_setaddr(t, efuse.mac_bytes)
        phy_init(t, mcu, efuse)
        state["marks"]["C"] = dev.i

    try:
        _drive()
    except rp.Divergence as e:
        _print_marks(state["marks"], anchor, dev.i, dev)
        names = {"A+B": "CHECK A+B (cold reset + firmware upload)",
                 "C": "CHECK C (post-FW init)"}
        print(f"  [FAIL] {names[state['phase']]} DIVERGENCE - {e}")
        print("         (cursor stops at the first op the driver does not reproduce; a genuine")
        print("          driver-vs-wire divergence, left red and localized per the gate mandate)")
        return "fail"
    except Exception as e:                       # driver raised (e.g. poll ran dry)
        _print_marks(state["marks"], anchor, dev.i, dev)
        print(f"  [FAIL] {state['phase']}: driver raised {type(e).__name__}: {e}")
        return "fail"

    _print_marks(state["marks"], anchor, dev.i, dev)
    print(f"  [PASS] CHECK A-C reproduced {dev.i - anchor} cold-boot + post-FW ops "
          f"byte-for-byte (single cursor)")
    return "pass"


def _print_marks(marks: dict, anchor: int, end: int, dev=None) -> None:
    ab = marks.get("AB")
    c = marks.get("C")
    if ab is not None:
        print(f"  [PASS] CHECK A+B (cold reset + firmware upload): {ab - anchor} ops")
    if c is not None:
        print(f"  [PASS] CHECK C (post-FW init): {c - ab} ops")
    if dev is not None:
        for addr, n in sorted(dev.extra_hits.items()):
            print(f"  [note] served {n} off-cursor read(s) of 0x{addr:08x} (wifit3 "
                  f"load_firmware warm-chip check; absent from the kernel cold-boot wire)")


# ---------------------------------------------------------------------------
# CHECK D — TX descriptor faithfulness (2.4 GHz inject only).
# ---------------------------------------------------------------------------

def check_tx(data: dict) -> str:
    seen_fw = False
    txs = []   # (ep, wire, frame, ack)
    for o in data["host_ops"]:
        if o["kind"] != "bulk":
            continue
        ep, wire = o["ep"], o["data"]
        if ep == rp.EP_MCU_OUT:
            seen_fw = True       # MCU/FW traffic precedes any inject
            continue
        if not (seen_fw and ep in (0x07, 0x04)):
            continue
        if len(wire) < _DMA_INFO_LEN + _TXWI_LEN + 2:
            continue
        txwi = wire[_DMA_INFO_LEN:_DMA_INFO_LEN + _TXWI_LEN]
        frame_len = struct.unpack_from("<H", txwi, 6)[0]
        ack = bool(txwi[4] & 0x01)
        if frame_len < 10 or _DMA_INFO_LEN + _TXWI_LEN + frame_len > len(wire):
            continue
        frame = wire[_DMA_INFO_LEN + _TXWI_LEN:_DMA_INFO_LEN + _TXWI_LEN + frame_len]
        txs.append((ep, wire, frame, ack))

    print("CHECK D - TX descriptor (mt76x02 DMA-info + TXWI, 2.4 GHz)")
    if not txs:
        print("  [UNVERIFIED] no post-boot 802.11 TX in this capture (stopped before aireplay)")
        return "skip"

    # wifit3's inject_80211_frame always uses EP 0x07 (AC_VO); EP 0x04 frames are
    # aireplay-generated on AC_BE, not a wifit3 inject path. The TXWI rate field
    # (packet bytes 6:8) is an intentional wifit3 deviation: build_txwi forces OFDM
    # 6 Mbps (0x2000) to dodge the chip's silent-drop of rate=0 (tx.py), while the
    # capture's aireplay TX used rate=0 (auto). A frame that matches except those two
    # bytes is counted as a rate-only deviation, not a build divergence.
    inj_ok = True
    inj_n = other_n = exact_n = rate_only_n = 0
    by_kind = Counter()
    first_bad = None
    for ep, wire, frame, ack in txs:
        by_kind[_FC_NAME.get(frame[0] & 0xFC, f"0x{frame[0]:02x}")] += 1
        if ep != 0x07:
            other_n += 1
            continue
        inj_n += 1
        built = bytes(mt_tx.build_inject_packet(frame, request_ack=ack, wcid=0xFF))
        wire = bytes(wire)
        if built == wire:
            exact_n += 1
            continue
        if (len(built) == len(wire) and built[:6] == wire[:6]
                and built[8:] == wire[8:]):
            rate_only_n += 1                 # only the TXWI rate field differs
            continue
        inj_ok = False
        if first_bad is None:
            n = min(len(built), len(wire))
            d = next((i for i in range(n) if built[i] != wire[i]), n)
            first_bad = (f"ep=0x{ep:02x} fc0=0x{frame[0]:02x} byte {d}: built "
                         f"{built[d:d+4].hex() if d < len(built) else '-'} vs wire "
                         f"{wire[d:d+4].hex() if d < len(wire) else '-'} "
                         f"(len {len(built)} vs {len(wire)})")
    print(f"  {len(txs)} TX frames ({inj_n} on EP 0x07 inject, {other_n} on EP 0x04 "
          f"aireplay-data): {dict(by_kind)}")
    if not inj_ok:
        print(f"  [FAIL] {first_bad}")
        return "fail"
    print(f"  [PASS] all {inj_n} wifit3-inject frames (EP 0x07) rebuilt faithfully "
          f"({exact_n} byte-exact, {rate_only_n} match except the TXWI rate field)")
    if rate_only_n:
        print("  [note] the rate-field diffs are the intentional OFDM-6Mbps override "
              "(wifit3 0x2000 vs wire 0x0000 auto; tx.py) - reported, not a port fix.")
    return "pass"


def run(cap=None) -> int:
    """Dispatcher entry. 0 = full green, 1 = divergence/failure, 2 = incomplete."""
    _patch_sleeps()
    cap = cap or DEFAULT_CAP
    cap_path = cap if Path(cap).is_absolute() else str(REPO / cap)
    pkts = rp.parse_pcapng(cap_path)
    if not pkts:
        print(f"[FAIL] no packets parsed from {cap}")
        return 1
    dev = rp.detect_card(pkts)
    if dev is None:
        print(f"[FAIL] no mt76-USB card (0x06/0x07 vendor traffic) found in {cap}")
        return 1
    data = rp.extract(pkts, dev)
    print(f"verify_pcap mt76x0u - {cap}")
    print(f"  {len(pkts)} packets; card auto-detected as device {dev}")
    print(f"  ops: {len(data['host_ops'])} positional, {len(data['responses'])} "
          f"MCU responses\n")

    print("CHECK A-C - cold reset + firmware + post-FW init (single cursor)")
    boot_verdict = check_boot(data)
    print()
    tx_verdict = check_tx(data)

    if boot_verdict == "fail" or tx_verdict == "fail":
        print("\n[FAIL] see localized divergence(s) above")
        return 1
    if tx_verdict == "skip":
        print("\n[INCOMPLETE] boot verified; CHECK D TX UNVERIFIED - no post-boot TX in "
              "this capture. NOT a full pass.")
        return 2
    print("\n[PASS] all checks green")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
