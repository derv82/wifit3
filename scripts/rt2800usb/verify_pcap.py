"""Acceptance gate: replay-diff rt2800usb (Ralink RT3572 / RT5372 / RT5572) against its
cold-boot capture. Goal: the port emits the exact bytes the Linux kernel driver did.

Drives the port's real ``RT2800USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so the port's real helpers
run unchanged and must emit byte-identical writes or a Divergence is raised at the first
mismatch.

Two sections, both fail-closed with 0 waivers:

  * [cold bring-up walk]  a SINGLE-CURSOR walk from the first vendor op (MAC_CSR0), driving the
                          port's real helpers in the kernel's wire order: read_chip_id →
                          read_eeprom_efuse (autorun + EFUSE loop) → probe_hw_gpio →
                          probe_hw_mode (xtal) → load_firmware. Catches wire-ORDER divergences,
                          not just per-block byte errors — it surfaced the AUTOWAKEUP_CFG
                          address bug (was MAC_BSSID_DW0) and the missing autorun_detect /
                          probe_hw GPIO ops. The steps after load_firmware (radio-on MCU_LED →
                          init_registers → init_bbp → init_rfcsr → enable_radio + monitor setup)
                          are still being converged; the walk names the exact FRONTIER op.
  * [channel tune]        every RF55xx (RT5572) config_channel + config_txpower, anchored at
                          each LDO_CFG0 RMW that opens a tune and reverse-mapped to its channel
                          from the RFCSR8 (N) load. Reaches the RFCSR49/50 (analog PA) +
                          TX_PWR_CFG_0..4 (per-rate) writes — the EEPROM-TX-power surface the
                          live EFUSE byte-gate never touched, which is why the min-power bug
                          survived. Requires a burned EEPROM + a channel sweep in the capture.

The cold walk and the tune blocks don't yet meet in the middle; that gap is being converged
op-by-op (the FRONTIER), NOT waived. Exit is 0 only once the cold walk reaches the operational
phase with no divergence.

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
from wifit3.chips.rt2800usb import firmware as _fw  # noqa: E402
from wifit3.chips.rt2800usb import mac as _mac  # noqa: E402
from wifit3.chips.rt2800usb import reg_init as _reg  # noqa: E402
from wifit3.chips.rt2800usb.constants import (  # noqa: E402
    LDO_CFG0,
    MAC_CSR0,
    MAC_DEBUG_INDEX,
    MAC_DEBUG_INDEX_XTAL,
    MCU_WAKEUP,
    RF_CSR_CFG,
    RF_CSR_CFG_WRITE,
    RT_RT5592,
)
from wifit3.chips.rt2800usb.eeprom import (  # noqa: E402
    EEPROM_OFFSET_FREQ,
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


class _Walk:
    """One cursor over the capture from the first vendor op. ``run`` drives a real port
    helper against the wire from the cursor (a fresh ReplayDevice over the remaining ops in
    the real transport) and advances by however many ops it reproduced; a Divergence stops
    the walk at the exact frontier -- the first op the port did NOT reproduce."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0

    def run(self, fn) -> int:
        rd = rp.ReplayDevice(self.ops[self.i:])
        fn(RT2800USBTransport(rd))
        self.i += rd.i
        return rd.i


def verify_cold_walk(pcap: Path, dev: int, silicon: int):
    """Single-cursor walk of the cold bring-up in the kernel's exact wire order, fail-closed:
    read_chip_id -> read_eeprom_efuse (autorun + EFUSE loop) -> probe_hw_gpio -> probe_hw_mode
    (xtal) -> load_firmware. Every op must reproduce byte-for-byte or the walk stops and names
    the frontier (the next kernel op to port). No anchoring, no waivers -- unlike the old
    anchored EFUSE/FW blocks, this catches wire-ORDER divergences (it already surfaced the
    AUTOWAKEUP_CFG address bug + the missing autorun_detect / probe_hw GPIO ops)."""
    print("\n[cold bring-up walk]")
    allops = rp.extract_ops(pcap, dev)
    anchor = next((i for i, o in enumerate(allops)
                   if o["dir"] == "IN" and o["addr"] == MAC_CSR0), 0)
    w = _Walk(allops[anchor:])
    fw = load_firmware_blob()
    box: dict = {"ev": None}

    def step(label, fn):
        n = w.run(fn)
        print(f"  OK    {label:44} +{n:4} ops  (cursor {anchor + w.i})")

    try:
        step("read_chip_id (MAC_CSR0)", lambda t: _mac.read_chip_id(t))
        step("read_eeprom_efuse (autorun + EFUSE loop)",
             lambda t: box.__setitem__("ev", parse_eeprom(read_eeprom_efuse(t))))
        ev = box["ev"]
        step("probe_hw_gpio (rfkill GPIO_CTRL_DIR2)", lambda t: _mac.probe_hw_gpio(t))
        step("probe_hw_mode (xtal read)", lambda t: _chan.is_xtal_40mhz(t))
        step("load_firmware (autorun + blob + MCU boot)",
             lambda t: _fw.load_firmware(t, fw, silicon_id=silicon, progress_cb=None))
        step("set_radio_led (MCU_LED, radio on)",
             lambda t: _mac.set_radio_led(t, ev.word(EEPROM_OFFSET_FREQ)))
        step("mcu_wakeup (MCU_WAKEUP)",
             lambda t: _fw.mcu_request(t, MCU_WAKEUP, token=0xFF, arg0=0, arg1=2))
        step("usb_enable_radio_dma (USB_DMA_CFG)", lambda t: _mac.usb_enable_radio_dma(t))
        step("wait_wpdma (rt2800_enable_radio)", lambda t: _mac._wait_wpdma_ready(t))
        step("init_registers (disable_wpdma+usb_reset+MAC block)",
             lambda t: _reg.init_registers(t, silicon))
    except rp.Divergence as e:
        fr = w.ops[w.i] if w.i < len(w.ops) else None
        print(f"  STOP  (diverged)\n        {e}")
        print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
              f"{' = ' + rp.ReplayDevice._fmt(fr) if fr else ''}.")
        return False, w.i, box["ev"]
    # All scripted steps reproduced. The cold bring-up is not finished here — the
    # remaining radio-on / init_registers / init_bbp / init_rfcsr / enable_radio steps are
    # still being converged, so the next op is the porting frontier.
    fr = w.ops[w.i] if w.i < len(w.ops) else None
    print(f"  reproduced {w.i} ops single-cursor byte-for-byte (0 waived).")
    print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
          f"{' = ' + rp.ReplayDevice._fmt(fr) if fr else ''} (radio-on → init_registers …).")
    return False, w.i, box["ev"]


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


def verify_channel_tune(pcap: Path, dev: int, silicon: int, ev) -> bool:
    """Anchored block(s): replay the port's real ``chan.set_channel`` (config_channel
    + config_txpower) against every RF55xx channel tune in the capture and require a
    byte-for-byte match. This is the gate that reaches the RFCSR49/50 (analog PA) +
    TX_PWR_CFG_0..4 (per-rate) writes the live EFUSE byte-gate never touches -- the
    surface the EEPROM TX-power bug hid behind. Each tune is reverse-mapped to its
    channel from the RFCSR8 (N) load + LDO VLEVEL band, then driven with the same
    EEPROM-derived kwargs driver._channel_kwargs() builds. ``ev`` is the EEPROM the
    cold walk already decoded from this capture."""
    print("\n[channel tune]")
    if silicon != RT_RT5592 or ev is None:
        print(f"  SKIP: RF55xx tune replay only (capture silicon 0x{silicon:04x}); "
              "the RF3052/RF53xx tune paths need their init-derived calibration state.")
        return True, 0
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

    cold_ok, cold_n, ev = verify_cold_walk(pcap, dev, silicon)
    tune_ok, tune_n = verify_channel_tune(pcap, dev, silicon, ev)

    walked = cold_n + tune_n
    total = len(allops)
    print("\n[coverage]")
    print(f"  {walked} / {total} vendor ops verified byte-for-byte ({100 * walked / total:.1f}%), "
          f"0 waived.")
    print(f"  cold bring-up: single-cursor walk to op {cold_n}"
          f"{' (COMPLETE)' if cold_ok else ' (FRONTIER — convergence in progress)'};"
          f" channel tunes: {tune_n} ops (anchored per-hop replay).")
    if not cold_ok:
        print("  The gap between the cold frontier and the tune blocks (radio-on / init_registers /")
        print("  init_bbp / init_rfcsr / enable_radio + monitor setup) is being converged to the")
        print("  kernel op-by-op; it is NOT waived. See the FRONTIER line above for the next op.")
    return 0 if (cold_ok and tune_ok) else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
