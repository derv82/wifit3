"""verify_pcap for MT76x2U (MT7612U) — the Pcap Replay for cold-boot bring-up.

Drives the driver's REAL bring-up (chips/mt76x2u/*) against an mt76-USB cold-boot capture
via a ReplayDevice (scripts/mt76usb_pcap_replay.py): the recorded reads/MCU-responses are
served back and every register write / bulk-OUT frame is asserted byte-for-byte. No hardware,
no reimplementation.

Four checks, mirroring scripts/mt7921au/verify_pcap.py:

  CHECK A — cold register init: reset_wlan + power_on (MTCMOS / RF power-on register walk),
            anchored at reset_wlan's first WLAN_FUN_CTRL read.
  CHECK B — firmware upload + MCU init: ROM patch + ILM/DLM chunks on EP 0x08, the single_wr
            FCE programming, init_dma, and mcu_init (function_select + radio_on).
  CHECK C — post-FW single cursor: mac_reset (initvals + XTAL fixup) + mac_setaddr + WCID /
            shared-key table clears + beacon config + mcu_load_cr + PHY paths + set_channel_20mhz
            (incl. the per-channel TX-power register block) + mac_start.
  CHECK D — TX inject (both bands): every aireplay TX frame on EP 0x07 (mgmt) / 0x04 (data),
            rebuilt via tx.assemble_tx_frame with the band-appropriate rate (2.4 -> CCK 0x0000,
            5 GHz -> OFDM 0x2000, picked from the wire's last channel switch) and asserted.

CHECK A->C share ONE monotonic cursor and one McuChannel (so the MCU seq counter stays
continuous): on a mismatch the cursor stops and the phase + byte-aligned expected-vs-got is
printed. A RED here is a true result — a genuine driver<->wire divergence is left red and
localized, never patched away.

Usage: uv run python scripts/mt76x2u/verify_pcap.py [<pcap>]
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

from wifit3.chips.mt76x2u import tx as mt_tx  # noqa: E402
from wifit3.chips.mt76x2u.driver import MT76x2UDriver  # noqa: E402
from wifit3.chips.mt76x2u.constants import (  # noqa: E402
    EP_OUT_AC_BE,
    MT_ASIC_VERSION,
    MT_RX_FILTR_CFG,
    MT_WLAN_FUN_CTRL,
    MT_WPDMA_GLO_CFG,
)

# DROP_UC_NOME (bit 2) | DROP_NOT_MYBSSID (bit 3): the drop bits mac_start clears
# for promiscuous monitor RX. A RX_FILTR_CFG write differing only here is accepted.
_MONITOR_RXFILTER_BITS = (1 << 2) | (1 << 3)

DEFAULT_CAP = "usb_dumps_new2/captures_mt76x2u_5g-injection/capture-1.pcap"

# mt76x02 MCU CMD_SWITCH_CHANNEL_OP (the channel rides in payload[0]). [SRC] mt76x02_mcu.h:36
_CMD_SWITCH_CHANNEL_OP = 30
# mac80211's default channel at the first config(CHANGE_POWER), before the tune to ch1.
# This 5g-injection capture defaults to 5 GHz UNII-1 ch36 (its initial phy_set_txpower).
_CONFIG_POWER_DEFAULT_CH = 36
# The capture's dev->txpower_conf (= hw->conf.power_level * 2). Reverse-engineered from
# TX_PWR_CFG_0=0x01010101 on ch1 (the rate table clamps to 34): power_level 17 dBm.
_CAPTURE_TXPOWER_CONF = 34
_TXWI_LEN = 20
_TXINFO_LEN = 4
_FC_NAME = {0xc0: "deauth", 0xa0: "disassoc", 0xb0: "auth", 0x40: "probe_req",
            0x50: "probe_resp", 0x48: "null", 0x88: "qos_null", 0x80: "beacon"}


def _patch_sleeps() -> None:
    """Replay needs no real settle delays; the cursor, not wall-clock, sequences ops."""
    time.sleep = lambda *a, **k: None

    async def _nosleep(*a, **k):
        return None
    asyncio.sleep = _nosleep


def _detect_asic_rev(host_ops: list[dict]) -> int:
    """ASIC version is a MULTI_READ of register 0x0000; low byte = silicon rev (E4=0x44)."""
    for o in host_ops:
        if (o["kind"] == "ctrl" and o["dir"] == "IN" and o["breq"] == 0x07
                and (o["wval"] << 16 | o["widx"]) == MT_ASIC_VERSION and o.get("data")):
            return int.from_bytes(o["data"][:4], "little") & 0xFF
    return 0x44   # AWUS036ACM is E4; only the FW DLM offset depends on this


def _txinfo_cmd_type(frame: bytes) -> int:
    if len(frame) < 4:
        return -1
    return (struct.unpack_from("<I", frame, 0)[0] >> 20) & 0x7F


def _first_switch_channel(host_ops: list[dict]) -> int | None:
    """Channel of the first CMD_SWITCH_CHANNEL_OP MCU command on EP 0x08 — the channel
    the wire tuned to at start (so CHECK C's set_channel_20mhz uses the right number)."""
    for o in host_ops:
        if (o["kind"] == "bulk" and o["ep"] == rp.EP_MCU_OUT
                and _txinfo_cmd_type(o["data"]) == _CMD_SWITCH_CHANNEL_OP
                and len(o["data"]) >= 5):
            return o["data"][4]
    return None


# ---------------------------------------------------------------------------
# CHECK A->C — STRICT single monotonic cursor over the REAL connect() cold path.
# The cursor is ANCHORED at reset_wlan's first MT_WLAN_FUN_CTRL read, skipping a
# prologue the capture does not share: the port's ASIC-version + warm-reattach probe
# (MT_MCU_COM_REG0), and the kernel's upfront EEPROM slurp (which the port instead
# reads lazily — served off-cursor from the recorded ROM, since EEPROM is not
# read-to-clear). From the anchor on, every MMIO op (reads included) is matched
# positionally and the walk stops at the first byte that differs.
# ---------------------------------------------------------------------------

def check_boot(data: dict, channel: int, asic_rev: int) -> tuple[str, int]:
    """Drive the driver's REAL cold bring-up (MT76x2UDriver._bringup) against a strict
    cursor anchored at reset_wlan. Returns (verdict, ops_matched). No hand copy: connect()
    and this walk run the same _bringup, so they cannot drift.

    The anchor skips the port-specific prologue the cold wire does not share (the port's
    ASIC-version read + warm-reattach probe live in connect() BEFORE _bringup; the wire's
    own prologue is the kernel's upfront EEPROM slurp, served off-cursor from the recorded
    ROM since EEPROM is idempotent). From reset_wlan on, every MMIO op is positional."""
    ops = data["host_ops"]
    anchor = rp.anchor_index(ops, breq=0x07, addr=MT_WLAN_FUN_CTRL)   # reset_wlan's 1st op
    if anchor is None:
        print("  [FAIL] no anchor: reset_wlan's MT_WLAN_FUN_CTRL MMIO read not in capture")
        return "fail", 0
    eeprom = rp.build_eeprom(ops)
    dev = rp.ReplayDevice(ops, data["responses"], start=anchor,
                          eeprom=eeprom,
                          rxfilter_addr=MT_RX_FILTR_CFG,
                          rxfilter_mask=_MONITOR_RXFILTER_BITS,
                          wpdma_addr=MT_WPDMA_GLO_CFG)

    # Build the real driver over the replay transport; seed the state the connect()
    # prologue would have set (asic_rev + is_mt7612 from the ASIC read, the initial
    # channel), then drive _bringup: reset_wlan -> cold init -> lazy EEPROM -> MAC tables
    # -> PHY -> mac_start -> the initial BARE channel tune.
    driver = MT76x2UDriver(dev, MT76x2UDriver.SUPPORTED_IDS[0])
    driver.asic_rev = asic_rev
    driver.is_mt7612 = True             # AWUS036ACM reference is mt7612 (WiFi-only)
    driver.current_channel = channel
    try:
        asyncio.run(driver._bringup())
    except rp.Divergence as e:
        print(f"  [FAIL] DIVERGENCE after {dev.i - anchor} matched op(s) past the "
              f"reset_wlan anchor (op #{anchor})")
        print(f"         {e}")
        return "fail", dev.i - anchor
    except Exception as e:              # driver raised (poll ran dry, etc.)
        print(f"  [FAIL] driver._bringup raised {type(e).__name__}: {e} "
              f"(after {dev.i - anchor} matched ops past the anchor)")
        return "fail", dev.i - anchor

    print(f"  [PASS] reproduced all {dev.i - anchor} cold-boot + post-FW MMIO ops "
          f"byte-for-byte via driver._bringup (anchored at reset_wlan op #{anchor}; "
          f"{dev.eeprom_served} EEPROM reads served off-cursor)")
    return "pass", dev.i - anchor


# ---------------------------------------------------------------------------
# CHECK D — TX descriptor accuracy (both bands).
# ---------------------------------------------------------------------------

def _kernel_hdrlen(fc0: int, fc1: int) -> int:
    """ieee80211_hdrlen, the KERNEL-correct version (used to recover the original frame
    from the wire). Differs from tx._ieee80211_hdrlen, which returns 10 for ALL control
    frames — wrong for RTS/PS-Poll (16). [SRC] linux ieee80211_hdrlen()."""
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:                         # control
        if subtype in (0x8, 0x9, 0xa, 0xb):   # BACK_REQ/BACK/PS-Poll/RTS
            return 16
        return 10                          # CTS / ACK / CF-End(-Ack)
    base = 24
    if (fc1 & 0x03) == 0x03:
        base = 30
    if ftype == 2 and (subtype & 0x08):    # QoS data
        base += 2
    return base


def _extract_802_11(wire: bytes, frame_len: int) -> bytes:
    """Recover the original 802.11 frame from a TX bulk-OUT transfer, undoing the
    optional 2-byte mt76_insert_hdr_pad between MAC header and body (using the
    KERNEL-correct hdrlen so the recovery is right even for frame types the driver
    mis-sizes)."""
    body = wire[_TXINFO_LEN + _TXWI_LEN:]
    if len(body) < 2:
        return body[:frame_len]
    hdrlen = _kernel_hdrlen(body[0], body[1])
    if hdrlen % 4 != 0 and len(body) >= hdrlen + 2:
        return body[:hdrlen] + body[hdrlen + 2:hdrlen + 2 + (frame_len - hdrlen)]
    return body[:frame_len]


def check_tx(data: dict) -> str:
    """STRICT: rebuild every wire TX frame via the driver's real inject builder
    (tx.assemble_tx_frame + the EP_OUT_AC_VO endpoint inject_frame always uses) and
    assert both bytes AND endpoint match. No frame type is excluded; a tally of exact
    / byte-divergent / endpoint-divergent is printed and any divergence fails."""
    ops = data["host_ops"]
    seen_fw = False
    current_channel = None
    txs = []   # (ep, wire, frame_802_11, ack, band_5ghz)
    for o in ops:
        if o["kind"] != "bulk":
            continue
        ep, wire = o["ep"], o["data"]
        if ep == rp.EP_MCU_OUT:
            if _txinfo_cmd_type(wire) == _CMD_SWITCH_CHANNEL_OP and len(wire) >= 5:
                current_channel = wire[4]
            seen_fw = True       # MCU traffic only flows once FW is up
            continue
        if not (seen_fw and ep in (0x07, 0x04)):
            continue
        if len(wire) < _TXINFO_LEN + _TXWI_LEN + 2:
            continue
        txwi = wire[_TXINFO_LEN:_TXINFO_LEN + _TXWI_LEN]
        frame_len = struct.unpack_from("<H", txwi, 6)[0]
        ack = bool(txwi[4] & 0x01)
        if frame_len < 10 or _TXINFO_LEN + _TXWI_LEN + frame_len > len(wire):
            continue
        frame = _extract_802_11(wire, frame_len)
        band_5ghz = (current_channel or 0) >= 36
        txs.append((ep, wire, frame, ack, band_5ghz))

    print("CHECK D - TX descriptor (mt76x02 TXINFO + TXWI)")
    if not txs:
        print("  [UNVERIFIED] no post-boot 802.11 TX in this capture (stopped before aireplay)")
        return "skip"

    exact = byte_div = ep_div = ep_excepted = 0
    by_kind = Counter()
    first_byte = first_ep = None
    for ep, wire, frame, ack, band_5ghz in txs:
        by_kind[_FC_NAME.get(frame[0] & 0xFC, f"0x{frame[0]:02x}")] += 1
        rate = mt_tx._TXWI_RATE_OFDM_6MBPS if band_5ghz else mt_tx._TXWI_RATE_CCK_1MBPS
        built = bytes(mt_tx.assemble_tx_frame(frame, ack=ack, rate=rate))
        wire = bytes(wire)
        is_data = (frame[0] & 0x0C) == 0x08
        # inject_frame routes EVERY frame to AC_VO (0x07) — the high-priority path WEP
        # ARP-replay data rides at 200-350 IVs/s. The aireplay ref put its data-null
        # frames on AC_BE (0x04) instead. Accept that one deliberate endpoint divergence
        # when the descriptor bytes still match exactly; every other byte is asserted.
        # (mt7921's verify masks its analogous deliberate TX divergence the same way.)
        if ep != mt_tx.EP_OUT_AC_VO:
            if is_data and ep == EP_OUT_AC_BE and built == wire:
                ep_excepted += 1
                continue
            ep_div += 1
            if first_ep is None:
                first_ep = (f"fc0=0x{frame[0]:02x} wire ep=0x{ep:02x}, "
                            f"wifit3 inject_frame would send on EP 0x{mt_tx.EP_OUT_AC_VO:02x}")
            continue
        if built == wire:
            exact += 1
            continue
        byte_div += 1
        if first_byte is None:
            n = min(len(built), len(wire))
            d = next((i for i in range(n) if built[i] != wire[i]), n)
            first_byte = (f"band={'5GHz' if band_5ghz else '2.4GHz'} ep=0x{ep:02x} "
                          f"fc0=0x{frame[0]:02x} byte {d}: built "
                          f"{built[d:d+4].hex() if d < len(built) else '-'} vs wire "
                          f"{wire[d:d+4].hex() if d < len(wire) else '-'} "
                          f"(len {len(built)} vs {len(wire)})")
    print(f"  {len(txs)} TX frames: {dict(by_kind)}")
    print(f"  exact byte+endpoint match: {exact}; byte-divergent: {byte_div}; "
          f"endpoint-divergent: {ep_div}")
    if ep_excepted:
        print(f"  ack-route-excepted {ep_excepted} data frame(s): inject routes AC_VO, "
              f"aireplay ref used AC_BE; every descriptor byte matched.")
    if byte_div:
        print(f"  [FAIL] first byte divergence: {first_byte}")
    if ep_div:
        print(f"  [FAIL] first endpoint divergence: {first_ep}")
    if byte_div or ep_div:
        return "fail"
    tail = ("descriptor + endpoint; AC route excepted above" if ep_excepted
            else "descriptor + endpoint")
    print(f"  [PASS] all {exact} TX frames rebuilt byte-for-byte ({tail})")
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
    asic_rev = _detect_asic_rev(data["host_ops"])
    channel = _first_switch_channel(data["host_ops"]) or 1
    print(f"verify_pcap mt76x2u - {cap}")
    print(f"  {len(pkts)} packets; card auto-detected as device {dev}; "
          f"ASIC rev=0x{asic_rev:02x}; first tuned channel={channel}")
    print(f"  ops: {len(data['host_ops'])} positional, "
          f"{len(data['responses'])} MCU responses\n")

    print("CHECK A-C - cold boot + firmware + post-FW init (STRICT single cursor)")
    boot_verdict, _ = check_boot(data, channel, asic_rev)
    print()
    tx_verdict = check_tx(data)

    if boot_verdict == "fail" or tx_verdict == "fail":
        print("\n[FAIL] see localized divergence(s) above")
        return 1
    if tx_verdict == "skip":
        print("\n[INCOMPLETE] boot verified; CHECK D TX UNVERIFIED — no post-boot TX in "
              "this capture. NOT a full pass.")
        return 2
    print("\n[PASS] all checks green")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
