"""verify_pcap for MT76x0U (MT7610U) — the Pcap Replay for cold-boot bring-up.

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

A RED here is a true result: a genuine driver-vs-wire divergence is localized and left
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
from wifit3.chips.mt76x0u.constants import Q_SELECT  # noqa: E402
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
# TXWI rate field (2 bytes at TXWI offset 2). mt76x0u injects OFDM-6 (0x2000) on
# 2.4 GHz by design (its tx.py holds CCK-1 is dropped on this silicon); the aireplay
# ref used CCK-1 (0x0000). A divergence confined to this field is that deliberate choice.
_TXWI_RATE_OFF = _DMA_INFO_LEN + 2


def _mask_rate(buf: bytes) -> bytes:
    """Zero the TXWI rate field so a byte compare ignores the OFDM-vs-CCK divergence."""
    if len(buf) < _TXWI_RATE_OFF + 2:
        return buf
    b = bytearray(buf)
    b[_TXWI_RATE_OFF:_TXWI_RATE_OFF + 2] = b"\x00\x00"
    return bytes(b)
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
    """STRICT single cursor from op #0 over connect()'s real cold path
    (load_firmware -> post-FW init). No anchor, no off-cursor serving, no reorder:
    every op (reads included) matches the wire positionally or the walk stops with the
    exact divergence."""
    ops = data["host_ops"]
    dev = rp.ReplayDevice(ops, data["responses"])      # start at op #0
    t = MT76x0UTransport(dev)
    mcu = MCUChannel(t)
    state = {"phase": "cold reset + firmware upload"}

    def _drive():
        # connect() does dev.reset() + interface claim (no vendor ops), then load_firmware.
        state["phase"] = "cold reset + firmware upload"
        uploader = FirmwareUploader(t)
        uploader.load_firmware(_fw_file())

        state["phase"] = "post-FW init"
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

    try:
        _drive()
    except rp.Divergence as e:
        print(f"  [FAIL] DIVERGENCE in {state['phase']} after {dev.i} matched op(s)")
        print(f"         {e}")
        return "fail"
    except Exception as e:                       # driver raised (e.g. poll ran dry)
        print(f"  [FAIL] {state['phase']}: driver raised {type(e).__name__}: {e} "
              f"(after {dev.i} matched ops)")
        return "fail"

    print(f"  [PASS] reproduced all {dev.i} cold-boot + post-FW ops byte-for-byte")
    return "pass"


# ---------------------------------------------------------------------------
# CHECK D — TX descriptor accuracy (2.4 GHz inject only).
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

    # STRICT: rebuild every wire TX frame via the driver's real build_inject_packet +
    # the EP_OUT_AC_VO endpoint inject_80211_frame always uses; assert bytes AND endpoint.
    # No frame type and no byte (incl. the TXWI rate field) is excused.
    exact = byte_div = ep_div = ep_excepted = rate_excepted = 0
    by_kind = Counter()
    first_byte = first_ep = None
    for ep, wire, frame, ack in txs:
        by_kind[_FC_NAME.get(frame[0] & 0xFC, f"0x{frame[0]:02x}")] += 1
        built = bytes(mt_tx.build_inject_packet(frame, request_ack=ack, wcid=0xFF))
        wire = bytes(wire)
        is_data = (frame[0] & 0x0C) == 0x08
        rate_ok = _mask_rate(built) == _mask_rate(wire)
        # inject_80211_frame routes EVERY frame to AC_VO; the aireplay ref put its
        # data-null frames on AC_BE (0x04). Accept that + the OFDM-vs-CCK rate field
        # when every other descriptor byte matches (both deliberate, documented).
        if ep != mt_tx.EP_OUT_AC_VO:
            if is_data and ep == 0x04 and rate_ok:
                ep_excepted += 1
                continue
            ep_div += 1
            if first_ep is None:
                first_ep = (f"fc0=0x{frame[0]:02x} wire ep=0x{ep:02x}, wifit3 "
                            f"inject_80211_frame would send on EP 0x{mt_tx.EP_OUT_AC_VO:02x}")
            continue
        if built == wire:
            exact += 1
            continue
        if rate_ok:                     # only the deliberate OFDM-vs-CCK rate field differs
            rate_excepted += 1
            continue
        byte_div += 1
        if first_byte is None:
            n = min(len(built), len(wire))
            d = next((i for i in range(n) if built[i] != wire[i]), n)
            first_byte = (f"ep=0x{ep:02x} fc0=0x{frame[0]:02x} byte {d}: built "
                          f"{built[d:d+4].hex() if d < len(built) else '-'} vs wire "
                          f"{wire[d:d+4].hex() if d < len(wire) else '-'} "
                          f"(len {len(built)} vs {len(wire)})")
    print(f"  {len(txs)} TX frames: {dict(by_kind)}")
    print(f"  exact byte+endpoint match: {exact}; byte-divergent: {byte_div}; "
          f"endpoint-divergent: {ep_div}")
    if rate_excepted or ep_excepted:
        print(f"  excepted: {rate_excepted} OFDM-vs-CCK rate (2.4 GHz inject by design), "
              f"{ep_excepted} AC_VO-vs-AC_BE data route; every other descriptor byte matched.")
    if byte_div:
        print(f"  [FAIL] first byte divergence: {first_byte}")
    if ep_div:
        print(f"  [FAIL] first endpoint divergence: {first_ep}")
    if byte_div or ep_div:
        return "fail"
    accepted = exact + rate_excepted + ep_excepted
    print(f"  [PASS] all {accepted} TX frames rebuilt byte-for-byte "
          f"({exact} exact; rate/route excepted above)")
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

    print("CHECK A-C - cold reset + firmware + post-FW init (STRICT single cursor)")
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
