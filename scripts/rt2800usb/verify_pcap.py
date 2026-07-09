"""Acceptance gate: replay-diff rt2800usb (Ralink RT3572 / RT5372 / RT5572) bring-up blocks
against its cold-boot capture.

Drives the port's real ``RT2800USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so the port's real helpers
run unchanged and must emit byte-identical writes or a Divergence is raised at the first
mismatch.

This is an *anchored-block* verifier (not the single-cursor whole-capture walk the clean-room
ports use): each block is extracted from the capture by an anchor predicate and replayed in
isolation, so coverage grows block by block without re-porting the whole driver. Blocks:

  * [EFUSE walk]      the 32-iteration EFUSE read loop (``read_eeprom_efuse``), anchored on
                      the first EFUSE_CTRL touch. Catches the ADDRESS_IN byte-vs-word bug.
  * [firmware upload] the rt2870.bin USB half (4096 bytes streamed to FIRMWARE_IMAGE_BASE
                      over 64-byte chunked control writes), anchored at the first chunk.
  * [channel tune]    every RF55xx (RT5572) config_channel + config_txpower, anchored at
                      each LDO_CFG0 RMW that opens a tune and reverse-mapped to its channel
                      from the RFCSR8 (N) load. Reaches the RFCSR49/50 (analog PA) +
                      TX_PWR_CFG_0..4 (per-rate) writes -- the EEPROM-TX-power surface the
                      live EFUSE byte-gate never touched, which is why the min-power bug
                      survived. Requires a burned EEPROM + a channel sweep in the capture.

Known divergence flagged for a separate session: the full ``load_firmware`` preamble opens
with ``write32(AUTOWAKEUP_CFG, 0)``, which this USB capture never issues -- that write is
PCI/SoC-only in rt2800lib.c, so the USB port should skip it. The blob upload below is
verified independently of that preamble.

Counter (2026-06-10): the "PCI/SoC-only" reading above is falsified -- ``rt2800lib.c:731``
writes ``AUTOWAKEUP_CFG`` *unconditionally*, above the ``is_pci`` guard (which holds only
``AUX_CTRL``/``PWR_PIN_CFG``). So the write matches the kernel and gating it out would be a
regression. The remaining open question is the wire-absence claim itself -- which this
*anchored-block* verifier can't settle, since it never replays the preamble. A single-cursor
full-walk would.

Run: uv run python scripts/rt2800usb/verify_pcap.py [capture-1|capture-2]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rt2x00_pcap_replay as rp  # noqa: E402
from wifit3.chips.rt2800usb import chan as _chan  # noqa: E402
from wifit3.chips.rt2800usb.constants import (  # noqa: E402
    FIRMWARE_IMAGE_BASE,
    LDO_CFG0,
    MAC_CSR0,
    MAC_DEBUG_INDEX,
    MAC_DEBUG_INDEX_XTAL,
    RF_CSR_CFG,
    RF_CSR_CFG_WRITE,
    RT_RT5592,
    USB_MULTI_WRITE,
)
from wifit3.chips.rt2800usb.eeprom import (  # noqa: E402
    EFUSE_CTRL,
    parse_eeprom,
    read_eeprom_efuse,
)
from wifit3.chips.rt2800usb.firmware import load_firmware_blob  # noqa: E402
from wifit3.chips.rt2800usb.transport import RT2800USBTransport  # noqa: E402

# Search the RT5572 (PAU09, full 2.4+5 GHz tune sweep) capture first, then the
# older RT5572- and RT5372-class ones.
CAP_DIRS = [
    REPO / "usb_dumps_new2" / "captures_rt2800usb_rt5572",  # RT5572 / PAU09, full sweep
    REPO / "usb_dumps" / "captures_rt2800usb_rt5372",       # RT5372 / PAU05-class
    REPO / "usb_dumps_new" / "captures_rt2800usb",          # RT5572 / PAU09-class
]


def _is_efuse_ctrl(o: dict) -> bool:
    """First touch of EFUSE_CTRL == rt2800_efuse_detect's PRESENT read, which opens the
    EFUSE read loop; anchor the block there so read_eeprom_efuse replays op-for-op."""
    return o["addr"] == EFUSE_CTRL


def _is_fw_chunk_start(o: dict) -> bool:
    """The firmware upload streams to FIRMWARE_IMAGE_BASE (0x3000) in 64-byte chunks at
    incrementing addresses; anchor on the first chunk (the base address)."""
    return (o["dir"] == "OUT" and o["breq"] == USB_MULTI_WRITE
            and o["addr"] == FIRMWARE_IMAGE_BASE)


def verify_efuse(pcap: Path, dev: int) -> bool:
    """Anchored block: replay the port's real ``read_eeprom_efuse`` against the capture's
    EFUSE read loop. The shipping reader writes ``EFUSE_CTRL.ADDRESS_IN`` as a BYTE offset,
    but the chip (and the kernel, ``rt2800lib.c`` ``i += 8`` words) treat it as a u16-WORD
    index -- so the buggy reader diverges on the 2nd iteration's KICK write. A word-offset
    reader (``ADDRESS_IN = offset // 2``) reproduces the whole loop byte-for-byte."""
    print("\n[EFUSE walk]")
    block = rp.extract_ops(pcap, dev, start=_is_efuse_ctrl)
    rd = rp.ReplayDevice(block)
    t = RT2800USBTransport(rd)
    try:
        eeprom = read_eeprom_efuse(t)
    except rp.Divergence as e:
        print(f"  FAIL (divergence): {e}")
        print(f"  reproduced {rd.i} EFUSE ops before diverging "
              f"-- the ADDRESS_IN byte-vs-word bug (see RT2800USB.md).")
        return False, rd.i
    ev = parse_eeprom(eeprom)
    print(f"  PASS: EFUSE read loop reproduced byte-for-byte over {rd.i} ctrl ops (0 waived).")
    print(f"  capture-unit decode: NIC_CONF0=0x{ev.nic_conf0:04x} "
          f"(rxpath={ev.rxpath} txpath={ev.txpath}), freq_offset={ev.freq_offset}, "
          f"lna_gain_bg=0x{ev.lna_gain_bg:02x}, rssi_bg0=0x{ev.rssi_bg_offset0:02x}")
    return True, rd.i


def verify_fw(pcap: Path, dev: int, fw: bytes) -> bool:
    """Anchored block: stream the bundled rt2870.bin and byte-verify it against the wire."""
    print("\n[firmware upload]")
    block = rp.extract_ops(pcap, dev, start=_is_fw_chunk_start)
    rd = rp.ReplayDevice(block)
    t = RT2800USBTransport(rd)
    try:
        t.write_multi(FIRMWARE_IMAGE_BASE, fw)   # 64-byte chunks, incrementing address
    except rp.Divergence as e:
        print(f"  FAIL (divergence): {e}")
        print(f"  reproduced {rd.i} firmware chunks before diverging")
        return False, rd.i
    print(f"  PASS: rt2870.bin upload verified byte-for-byte -- {len(fw)} bytes over {rd.i} "
          f"chunked control writes from 0x{FIRMWARE_IMAGE_BASE:04x} (0 waived).")
    print("  NOTE: load_firmware's preamble write32(AUTOWAKEUP_CFG, 0) is reportedly absent "
          "from this USB capture -- but the 'PCI/SoC-only' reading is falsified (rt2800lib.c:731 "
          "writes it unconditionally, above the is_pci guard). Disputed; this anchored verifier "
          "never walks the preamble, so a single-cursor full-walk is what would adjudicate it.")
    return True, rd.i


def _rfcsr8_write(o: dict):
    """If ``o`` is an OUT write to RF_CSR_CFG loading RFCSR8 (the synthesizer N
    low byte), return N&0xFF; else None."""
    if o["dir"] != "OUT" or o["addr"] != RF_CSR_CFG or len(o.get("data", b"")) != 4:
        return None
    v = int.from_bytes(o["data"], "little")
    if not (v & RF_CSR_CFG_WRITE) or ((v >> 8) & 0x3F) != 8:
        return None
    return v & 0xFF


def _find_rf55xx_tune_starts(ops: list[dict]) -> list[tuple[int, int, int]]:
    """Locate each rt2800_config_channel_rf55xx: an ``IN`` read of LDO_CFG0 whose
    ``OUT`` write is the very next op (the VLEVEL RMW that opens config_channel),
    followed within a few ops by the RFCSR8 (N) load. Returns (index, band, n_low)
    where band = LDO_CORE_VLEVEL (0 -> 2.4 GHz, 5 -> 5 GHz)."""
    starts = []
    for i in range(len(ops) - 6):
        if ops[i]["dir"] != "IN" or ops[i]["addr"] != LDO_CFG0:
            continue
        if ops[i + 1]["dir"] != "OUT" or ops[i + 1]["addr"] != LDO_CFG0:
            continue
        band = (int.from_bytes(ops[i + 1]["data"], "little") >> 26) & 0x7
        n_low = next((r for j in range(i + 2, min(i + 8, len(ops)))
                      if (r := _rfcsr8_write(ops[j])) is not None), None)
        if n_low is not None:
            starts.append((i, band, n_low))
    return starts


def verify_channel_tune(pcap: Path, dev: int, silicon: int) -> bool:
    """Anchored block(s): replay the port's real ``chan.set_channel`` (config_channel
    + config_txpower) against every RF55xx channel tune in the capture and require a
    byte-for-byte match. This is the gate that reaches the RFCSR49/50 (analog PA) +
    TX_PWR_CFG_0..4 (per-rate) writes the live EFUSE byte-gate never touches -- the
    surface the EEPROM TX-power bug hid behind. Each tune is reverse-mapped to its
    channel from the RFCSR8 (N) load + LDO VLEVEL band, then driven with the same
    EEPROM-derived kwargs driver._channel_kwargs() builds."""
    print("\n[channel tune]")
    if silicon != RT_RT5592:
        print(f"  SKIP: RF55xx tune replay only (capture silicon 0x{silicon:04x}); "
              "the RF3052/RF53xx tune paths need their init-derived calibration state.")
        return True, 0
    ev = parse_eeprom(read_eeprom_efuse(RT2800USBTransport(rp.ReplayDevice(
        rp.extract_ops(pcap, dev, start=lambda o: o["addr"] == EFUSE_CTRL)))))
    allops = rp.extract_ops(pcap, dev)
    mdi = next((o for o in allops if o["dir"] == "IN" and o["addr"] == MAC_DEBUG_INDEX), None)
    xtal = bool(int.from_bytes(mdi["data"], "little") & MAC_DEBUG_INDEX_XTAL) if mdi else False
    table = _chan._RF_VALS_5592_XTAL40 if xtal else _chan._RF_VALS_5592_XTAL20

    starts = _find_rf55xx_tune_starts(allops)
    matched, channels, walked = 0, set(), 0
    for (i, band, n_low) in starts:
        is_2g = (band == 0)
        # RFCSR8 = N&0xFF narrows to <=2 candidates (2.4 GHz ch pairs share N);
        # the wrong one diverges at the RFCSR9 K bits, so the clean replay IS the
        # channel. Pick the candidate whose set_channel consumes the most ops.
        cands = [ch for ch, v in table.items()
                 if (ch <= 14) == is_2g and (v[0] & 0xFF) == n_low]
        best_ch, best_consumed, best_err = None, -1, None
        for ch in cands:
            rd = rp.ReplayDevice(allops[i:])
            t = RT2800USBTransport(rd)
            p1, p2 = _chan.default_power(ev, RT_RT5592, ch, xtal)
            lna = ev.lna_gain_bg if ch <= 14 else ev.lna_gain_a
            err = None
            try:
                _chan.set_channel(
                    t, RT_RT5592, ch, freq_offset=ev.freq_offset, lna_gain=lna,
                    tx_chain_num=ev.txpath, rx_chain_num=ev.rxpath,
                    has_cap_bt_coexist=ev.has_cap_bt_coexist,
                    has_cap_external_lna_a=ev.has_cap_external_lna_a,
                    has_cap_external_lna_bg=ev.has_cap_external_lna_bg,
                    xtal_40mhz=xtal, iq_cal=ev.iq_cal,
                    default_power1=p1, default_power2=p2, eeprom=ev)
            except rp.Divergence as e:
                err = str(e)
            if rd.i > best_consumed:
                best_ch, best_consumed, best_err = ch, rd.i, err
        if best_err is None:
            matched += 1
            walked += best_consumed
            channels.add(best_ch)
        else:
            band_s = "2.4 GHz" if is_2g else "5 GHz"
            print(f"  FAIL: {band_s} tune (RFCSR8=0x{n_low:02x}, best guess ch{best_ch}) "
                  f"diverged after {best_consumed} ops:\n    {best_err}")
            return False, walked

    chans = sorted(channels)
    print(f"  PASS: {matched}/{len(starts)} RF55xx tune blocks reproduced byte-for-byte "
          f"over {walked} ops (config_channel + config_txpower, 0 waived), xtal{'40' if xtal else '20'}.")
    print(f"  channels covered: {chans}")
    print("  RFCSR49/50 (analog PA) + TX_PWR_CFG_0..4 (per-rate) verified against the "
          "burned EEPROM's per-channel TXPOWER_BG/A + BYRATE tables.")
    return True, walked


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    arg = cap or "capture-1"
    if arg.endswith(".pcap") or "/" in arg or "\\" in arg:   # explicit path (rel to REPO ok)
        pcap = Path(arg) if Path(arg).is_absolute() else REPO / arg
    else:                                                    # bare name: search known dirs
        pcap = next((d / f"{arg}.pcap" for d in CAP_DIRS if (d / f"{arg}.pcap").exists()), None)
    if pcap is None or not pcap.exists():
        print(f"FAIL: cannot find capture for '{arg}'")
        return 1
    name = pcap.stem
    print(f"using {pcap}")

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    allops = rp.extract_ops(pcap, dev)
    csr0 = next((o for o in allops if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    silicon = (int.from_bytes(csr0["data"], "little") >> 16) & 0xFFFF if csr0 else 0
    fw = load_firmware_blob()
    print(f"{name}: card=dev{dev}, {len(allops)} vendor ops, silicon=0x{silicon:04x}, "
          f"fw={len(fw)}B")

    efuse_ok, efuse_n = verify_efuse(pcap, dev)
    fw_ok, fw_n = verify_fw(pcap, dev, fw)
    tune_ok, tune_n = verify_channel_tune(pcap, dev, silicon)

    walked = efuse_n + fw_n + tune_n
    total = len(allops)
    print("\n[coverage]")
    print(f"  {walked} / {total} vendor ops walked byte-for-byte ({100 * walked / total:.1f}%), "
          f"0 waived.")
    print(f"  {total - walked} ops ({100 * (total - walked) / total:.1f}%) NOT walked (and not "
          "waived): the cold bring-up (init_registers / init_bbp / init_rfcsr / enable_radio +")
    print("  MAC config), the airmon/monitor setup, and per-hop the stop-rx / update-survey")
    print("  (pre-tune) and config_ant / reset_tuner (post-tune) tails this anchored-block gate")
    print("  brackets out. This is NOT a single-cursor whole-capture walk (cf. verify_pcap rt5370).")
    return 0 if (efuse_ok and fw_ok and tune_ok) else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
