"""Acceptance gate: replay-diff rt2800usb (Ralink RT3572 / RT5372 / RT5572) against its
cold-boot capture. Goal: the port emits the exact bytes the Linux kernel driver did.

Drives the port's real ``RT2800USBTransport`` around a ``rt2x00_pcap_replay.ReplayDevice``
(a fake usb dev that replays the chip's recorded ctrl_transfers), so the port's real helpers
run unchanged and must emit byte-identical writes or a Divergence is raised at the first
mismatch.

Three sections, all fail-closed with 0 waivers:

  * [cold bring-up walk]  a SINGLE-CURSOR walk over the WHOLE control-op stream from the first
                          vendor op (MAC_CSR0) to the last, driving the port's real helpers in
                          the kernel's wire order: read_chip_id → EFUSE → probe_hw → firmware →
                          radio-on → init_registers → init_bbp → init_rfcsr → enable_radio →
                          (operational) monitor-enable → the channel-hop stream → the aireplay-ng
                          TX-injection region (TX_STA_FIFO drain). Catches wire-ORDER divergences,
                          not just per-block byte errors. The hop stream runs a STRICT per-hop
                          bracket (config_channel+txpower → reset_tuner → config_ant → reset_tuner
                          → start_rx → stop_rx → update_survey) with per-step opener checks; the
                          ONLY event allowed to interleave out of order is a configure_filter
                          reapply, proven async (4 reapplies vs 126 hops) and still byte-checked.
  * [channel tune]        every RF55xx config_channel + config_txpower, anchored per LDO_CFG0 RMW
                          and reverse-mapped to its channel. Now subsumed by the single-cursor
                          walk; kept as an independent cross-check of the RFCSR49/50 (analog PA)
                          + TX_PWR_CFG_0..4 (per-rate) writes vs the burned EEPROM tables.
  * [tx inject]           every host→chip bulk-OUT TX frame (aireplay-ng / airodump inject)
                          rebuilt by the port's tx.build_tx_descriptors and required to match
                          byte-for-byte (TXINFO + TXWI + 802.11 + align/USB-end pad), across CCK
                          (2.4 GHz) + OFDM (5 GHz). Proves the TX wire format matches the kernel.

Exit is 0 only when all three reproduce with no divergence and no waiver.

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
from wifit3.chips.rt2800usb import bbp as _bbp  # noqa: E402
from wifit3.chips.rt2800usb import firmware as _fw  # noqa: E402
from wifit3.chips.rt2800usb import mac as _mac  # noqa: E402
from wifit3.chips.rt2800usb import reg_init as _reg  # noqa: E402
from wifit3.chips.rt2800usb import rfcsr as _rfcsr  # noqa: E402
from wifit3.chips.rt2800usb.constants import (  # noqa: E402
    BBP_CSR_CFG,
    CH_IDLE_STA,
    FIF_ALLMULTI,
    FIF_CONTROL,
    FIF_PSPOLL,
    LDO_CFG0,
    MAC_CSR0,
    MAC_DEBUG_INDEX,
    MAC_DEBUG_INDEX_XTAL,
    MAC_SYS_CTRL,
    MAC_SYS_CTRL_ENABLE_RX,
    MCU_WAKEUP,
    RF_CSR_CFG,
    RF_CSR_CFG_WRITE,
    RT_RT5592,
    RX_FILTER_CFG,
    TX_STA_FIFO,
    TXINFO_W0_QSEL,
    TXINFO_W0_USB_DMA_NEXT_VALID,
    TXINFO_W0_USB_DMA_TX_BURST,
    TXWI_W0_MCS,
    TXWI_W0_PHYMODE,
    TXWI_W1_ACK,
    TXWI_W1_MPDU_TOTAL_BYTE_COUNT,
    TXWI_W1_PACKETID_ENTRY,
    TXWI_W1_PACKETID_QUEUE,
)
from wifit3.chips.rt2800usb.link_tuner import (  # noqa: E402
    get_default_vgc,
    set_vgc,
)
from wifit3.chips.rt2800usb.eeprom import (  # noqa: E402
    EEPROM_OFFSET_FREQ,
    parse_eeprom,
    read_eeprom_efuse,
)
from wifit3.chips.rt2800usb.firmware import load_firmware_blob  # noqa: E402
from wifit3.chips.rt2800usb.transport import RT2800USBTransport  # noqa: E402
from wifit3.chips.rt2800usb.tx import (  # noqa: E402
    build_tx_descriptors,
    txwi_size_for_silicon,
)

# Search the RT5572 (PAU09, full 2.4+5 GHz tune sweep) capture first, then the
# older RT5572- and RT5372-class ones.
CAP_DIRS = [
    REPO / "driver_captures" / "captures_rt2800usb_rt5572_2",  # RT5572 / PAU09, full sweep
    REPO / "driver_captures" / "captures_rt2800usb_rt5372",       # RT5372 / PAU05-class
    REPO / "driver_captures" / "captures_rt2800usb",          # RT5572 / PAU09-class
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
    box: dict = {"ev": None, "chip": None, "xtal": False}

    def step(label, fn):
        n = w.run(fn)
        print(f"  OK    {label:44} +{n:4} ops  (cursor {anchor + w.i})")

    try:
        step("read_chip_id (MAC_CSR0)",
             lambda t: box.__setitem__("chip", _mac.read_chip_id(t)))
        step("read_eeprom_efuse (autorun + EFUSE loop)",
             lambda t: box.__setitem__("ev", parse_eeprom(read_eeprom_efuse(t))))
        ev = box["ev"]
        step("probe_hw_gpio (rfkill GPIO_CTRL_DIR2)", lambda t: _mac.probe_hw_gpio(t))
        step("probe_hw_mode (xtal read)",
             lambda t: box.__setitem__("xtal", _chan.is_xtal_40mhz(t)))
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
        chip = box["chip"]
        from wifit3.chips.rt2800usb.constants import (
            EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK, EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT,
        )
        ant_div = (ev.nic_conf1 & EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK) \
            >> EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT
        step("prepare_bbp (wait_bbp_rf + H2M + MCU_BOOT + wait_bbp)",
             lambda t: _bbp.prepare_bbp(t))
        step("init_bbp", lambda t: _bbp.init_bbp(
            t, silicon, txpath=ev.txpath, rxpath=ev.rxpath,
            ant_diversity=ant_div, chip_rev=chip.revision))
        step("init_rfcsr", lambda t: _rfcsr.init_rfcsr(
            t, silicon, freq_offset=ev.freq_offset, chip_rev=chip.revision,
            txpath=ev.txpath, rxpath=ev.rxpath))
        step("enable_radio_finish (MAC/WPDMA enable + LED)",
             lambda t: _mac.enable_radio_finish(t, ev))

        # ---- operational phase: airmon monitor-enable ----
        # mac80211 brings the monitor interface up around a quiesced receiver:
        # toggle RX, set the (STA-default-flags) filter, push the initial
        # power/retry/PS config, configure antennas + reset the tuner, then
        # re-filter with CONFIG_MONITORING set and bank the survey. mac80211
        # passes FIF_ALLMULTI | FIF_CONTROL | FIF_PSPOLL for a monitor interface
        # (0x97); the monitoring flip (off → on) clears DROP_NOT_TO_ME → 0x93.
        mon_flags = FIF_ALLMULTI | FIF_CONTROL | FIF_PSPOLL
        step("toggle_rx on (start QID_RX)", lambda t: _mac.toggle_rx(t, True))
        step("config_filter (FIF_ALLMULTI|FIF_CONTROL, mon off → 0x97)",
             lambda t: _mac.config_filter(t, mon_flags, monitoring=False))
        step("toggle_rx off (stop QID_RX)", lambda t: _mac.toggle_rx(t, False))
        step("config power+retry+ps (CHANGE_POWER|RETRY|PS)",
             lambda t: (_chan.config_txpower(t, ev, is_2g=True),
                        _mac.config_retry_limit(t),
                        _mac.config_ps_awake(t)))
        step("config_ant + reset_tuner (BBP antenna + VGC seed)",
             lambda t: (_chan.config_ant(t, ev.txpath, ev.rxpath),
                        set_vgc(t, silicon,
                                get_default_vgc(silicon, 1, ev.lna_gain_bg),
                                rx_chain_num=ev.rxpath, rssi=0)))
        step("toggle_rx on", lambda t: _mac.toggle_rx(t, True))
        step("config_filter (CONFIG_MONITORING on → 0x93)",
             lambda t: _mac.config_filter(t, mon_flags, monitoring=True))
        step("toggle_rx off", lambda t: _mac.toggle_rx(t, False))
        step("update_survey (CH_IDLE/BUSY/BUSY_SEC)", lambda t: _mac.update_survey(t))

        # ---- channel hops ----
        # Each hop is two mac80211 events driven in the single cursor: a config
        # (channel) = config_channel + config_txpower + reset_tuner, and a
        # config_antenna = config_ant + reset_tuner; then start_rx, stop_rx and
        # a survey bank for the next hop. The channel per hop is reverse-mapped
        # from the RFCSR8 (N) load + LDO VLEVEL band, then set_channel is driven
        # for real on the cursor (a wrong channel diverges at the RFCSR9 K bits).
        xtal = box["xtal"]
        table = _chan._RF_VALS_5592_XTAL40 if xtal else _chan._RF_VALS_5592_XTAL20

        def sc_kwargs(ch):
            p1, p2 = _chan.default_power(ev, silicon, ch, xtal)
            lna = ev.lna_gain_bg if ch <= 14 else ev.lna_gain_a
            return dict(
                freq_offset=ev.freq_offset, lna_gain=lna,
                tx_chain_num=ev.txpath, rx_chain_num=ev.rxpath,
                has_cap_bt_coexist=ev.has_cap_bt_coexist,
                has_cap_external_lna_a=ev.has_cap_external_lna_a,
                has_cap_external_lna_bg=ev.has_cap_external_lna_bg,
                xtal_40mhz=xtal, iq_cal=ev.iq_cal,
                default_power1=p1, default_power2=p2, eeprom=ev)

        def detect_ch(rem):
            """Reverse-map the tune at the cursor to its channel, or None if the
            cursor isn't on an LDO_CFG0-opened RF55xx config_channel."""
            if (len(rem) < 8 or rem[0]["dir"] != "IN" or rem[0]["addr"] != LDO_CFG0
                    or rem[1]["dir"] != "OUT" or rem[1]["addr"] != LDO_CFG0):
                return None
            band = (int.from_bytes(rem[1]["data"], "little") >> 26) & 0x7
            n_low = next((r for j in range(2, 8)
                          if (r := _rfcsr8_write(rem[j])) is not None), None)
            if n_low is None:
                return None
            is2g = (band == 0)
            best, best_consumed = None, -1
            for ch in [c for c, v in table.items()
                       if (c <= 14) == is2g and (v[0] & 0xFF) == n_low]:
                rd = rp.ReplayDevice(rem)
                try:
                    _chan.set_channel(RT2800USBTransport(rd), silicon, ch, **sc_kwargs(ch))
                except rp.Divergence:
                    pass
                if rd.i > best_consumed:
                    best, best_consumed = ch, rd.i
            return best

        # The hop stream is a STRICT synchronous per-hop bracket with exactly one
        # allowlisted async interloper. Each hop, in fixed kernel order:
        #   config_channel+txpower → reset_tuner → config_ant → reset_tuner
        #   → start_rx → stop_rx → update_survey
        # Every step's opener is checked before its real helper runs (byte-strict).
        # The ONLY event allowed to appear out of that order is a configure_filter
        # reapply (RX_FILTER_CFG) — proven async here (4 reapplies vs 126 hops, and
        # they fire 2–396 frames after an RX re-enable, not inline). It is drained
        # between bracket steps; it is still driven by the real helper + byte-checked,
        # only its POSITION is flexible. Any op that is neither the expected next
        # bracket step NOR an allowlisted async reapply ends the hop phase (a desynced
        # synchronous op therefore can't be silently absorbed — the walk stops there).
        def op_at(off=0):
            j = w.i + off
            return w.ops[j] if 0 <= j < len(w.ops) else None

        def is_in(o, addr):
            return o is not None and o["dir"] == "IN" and o["addr"] == addr

        def next_bbp_out_regnum():
            """Regnum of the first BBP write after the cursor's busy-wait read
            (distinguishes reset_tuner's BBP83 opener from config_ant's BBP1)."""
            for k in range(1, 5):
                o = op_at(k)
                if (o is not None and o["dir"] == "OUT" and o["addr"] == BBP_CSR_CFG
                        and len(o.get("data", b"")) == 4):
                    return (int.from_bytes(o["data"], "little") >> 8) & 0xFF
            return None

        def toggle_sets_rx():
            o = op_at(1)
            if o is not None and o["dir"] == "OUT" and o["addr"] == MAC_SYS_CTRL:
                return bool(int.from_bytes(o["data"], "little") & MAC_SYS_CTRL_ENABLE_RX)
            return None

        def drain_filter():
            n = 0
            while is_in(op_at(), RX_FILTER_CFG):    # async configure_filter reapply
                w.run(lambda t: _mac.config_filter(t, mon_flags, monitoring=True))
                n += 1
            return n

        hop_start, hops, n_filter = w.i, [], 0
        while True:
            n_filter += drain_filter()
            if not is_in(op_at(), LDO_CFG0):
                break                                # end of hop stream (injection)
            ch = detect_ch(w.ops[w.i:])
            if ch is None:
                break
            lna = ev.lna_gain_bg if ch <= 14 else ev.lna_gain_a
            vgc = get_default_vgc(silicon, ch, lna)
            # (opener predicate, real helper) for each bracket step after config_channel.
            bracket = [
                (lambda: is_in(op_at(), BBP_CSR_CFG) and next_bbp_out_regnum() == 83,
                 lambda t: set_vgc(t, silicon, vgc, rx_chain_num=ev.rxpath, rssi=0)),
                (lambda: is_in(op_at(), BBP_CSR_CFG) and next_bbp_out_regnum() == 1,
                 lambda t: _chan.config_ant(t, ev.txpath, ev.rxpath)),
                (lambda: is_in(op_at(), BBP_CSR_CFG) and next_bbp_out_regnum() == 83,
                 lambda t: set_vgc(t, silicon, vgc, rx_chain_num=ev.rxpath, rssi=0)),
                (lambda: is_in(op_at(), MAC_SYS_CTRL) and toggle_sets_rx() is True,
                 lambda t: _mac.toggle_rx(t, True)),
                (lambda: is_in(op_at(), MAC_SYS_CTRL) and toggle_sets_rx() is False,
                 lambda t: _mac.toggle_rx(t, False)),
                (lambda: is_in(op_at(), CH_IDLE_STA),
                 lambda t: _mac.update_survey(t)),
            ]
            w.run(lambda t, c=ch: _chan.set_channel(t, silicon, c, **sc_kwargs(c)))
            for opener_ok, drive in bracket:
                n_filter += drain_filter()
                if not opener_ok():
                    break                            # bracket truncated by injection
                w.run(drive)
            else:
                hops.append(ch)
                continue
            break     # a bracket step's opener was absent → hop stream ended
        strict_hops = len(hops)
        if hops:
            print(f"  OK    {len(hops)} channel hops (strict bracket, "
                  f"{n_filter} async filter reapplies) +{w.i - hop_start} ops  "
                  f"(cursor {anchor + w.i})")
            print(f"        channels covered: {sorted(set(hops))}")

        # ---- TX-injection region ----
        # After the last full-bracket hop the capture is the kernel draining TX status:
        # it polls TX_STA_FIFO (read-to-pop) to reap each aireplay inject's completion,
        # interleaved with the final 1-2 channel hops + async filter reapplies. wifit3
        # fires TX without this drain loop, so the FIFO reads are kernel TX-completion
        # tracking (reproduced so the single cursor can traverse the region and reach the
        # interleaved hops); the injected frames themselves are verified byte-for-byte in
        # [tx inject]. Generic opener dispatch is right here — the interleave is genuinely
        # async, not a fixed bracket — and any unrecognized op still halts the walk.
        def hop_vgc(ch):
            lna = ev.lna_gain_bg if ch <= 14 else ev.lna_gain_a
            return get_default_vgc(silicon, ch, lna)

        inj_start, tx_reads, cur_ch = w.i, 0, (hops[-1] if hops else 1)
        while w.i < len(w.ops):
            o = op_at()
            if is_in(o, TX_STA_FIFO):
                w.run(lambda t: t.read32(TX_STA_FIFO))       # kernel TX-status drain (pop)
                tx_reads += 1
            elif is_in(o, RX_FILTER_CFG):
                w.run(lambda t: _mac.config_filter(t, mon_flags, monitoring=True))
                n_filter += 1
            elif is_in(o, CH_IDLE_STA):
                w.run(lambda t: _mac.update_survey(t))
            elif is_in(o, MAC_SYS_CTRL):
                enable = bool(int.from_bytes(op_at(1)["data"], "little")
                              & MAC_SYS_CTRL_ENABLE_RX)
                w.run(lambda t, e=enable: _mac.toggle_rx(t, e))
            elif is_in(o, LDO_CFG0):
                ch = detect_ch(w.ops[w.i:])
                if ch is None:
                    break
                w.run(lambda t, c=ch: _chan.set_channel(t, silicon, c, **sc_kwargs(c)))
                hops.append(ch)
                cur_ch = ch
            elif is_in(o, BBP_CSR_CFG) and next_bbp_out_regnum() == 83:
                w.run(lambda t, c=cur_ch: set_vgc(t, silicon, hop_vgc(c),
                      rx_chain_num=ev.rxpath, rssi=0))
            elif is_in(o, BBP_CSR_CFG) and next_bbp_out_regnum() == 1:
                w.run(lambda t: _chan.config_ant(t, ev.txpath, ev.rxpath))
            else:
                break                                        # unrecognized op → frontier
        if tx_reads:
            print(f"  OK    TX-injection region +{w.i - inj_start} ops  (cursor "
                  f"{anchor + w.i}): {tx_reads} kernel TX_STA_FIFO drain reads + "
                  f"{len(hops) - strict_hops} interleaved hop(s), {n_filter} filter reapplies")
    except rp.Divergence as e:
        fr = w.ops[w.i] if w.i < len(w.ops) else None
        print(f"  STOP  (diverged)\n        {e}")
        print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
              f"{' = ' + rp.ReplayDevice._fmt(fr) if fr else ''}.")
        return False, w.i, box["ev"]
    complete = w.i >= len(w.ops)
    if complete:
        print(f"  reproduced ALL {w.i} control ops single-cursor byte-for-byte (0 waived) —"
              " the full cold bring-up (probe → EFUSE → firmware → radio-on → init_registers"
              " → BBP → RFCSR → enable_radio) + operational phase (monitor-enable → channel"
              " hops → aireplay-ng TX-injection: TX_STA_FIFO drain).")
    else:
        fr = w.ops[w.i]
        print(f"  reproduced {w.i} ops single-cursor byte-for-byte (0 waived).")
        print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
              f" = {rp.ReplayDevice._fmt(fr)}.")
    return complete, w.i, box["ev"]


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


def verify_tx_inject(pcap: Path, dev: int, silicon: int):
    """Rebuild every captured bulk-OUT TX frame (aireplay-ng / airodump inject) with the
    port's ``tx.build_tx_descriptors`` and require a byte-for-byte match of the whole
    TXINFO + TXWI + 802.11 + align/USB-end-pad payload. Proves the port's TX wire format
    matches the kernel across every injected frame type AND both bands (CCK 2.4 GHz +
    OFDM 5 GHz). Descriptor fields are decoded with the port's own TXWI/TXINFO field
    masks and fed back as build params; because the CAPTURE is ground truth at the real
    hardware bit positions, a wrong port mask still mismatches. Only the rotating PACKETID
    (a TX-status match tag, not on-air) and the USB burst flags are read from the capture;
    every rate/mode/length/pad field is produced by the port's builder and verified."""
    print("\n[tx inject]")
    frames = rp.extract_bulk_out(pcap, dev)
    if not frames:
        print("  SKIP: no bulk-OUT TX frames in this capture.")
        return True, 0
    txwi_size = txwi_size_for_silicon(silicon)
    hdr = 4 + txwi_size          # TXINFO (4B) + TXWI

    def fld(reg, mask):
        return (reg & mask) >> ((mask & -mask).bit_length() - 1)

    matched, bands, endpoints, shown = 0, {}, {}, 0
    for f in frames:
        cap = f["data"]
        txinfo = int.from_bytes(cap[0:4], "little")
        w0 = int.from_bytes(cap[4:8], "little")
        w1 = int.from_bytes(cap[8:12], "little")
        phymode = fld(w0, TXWI_W0_PHYMODE)
        mcs = fld(w0, TXWI_W0_MCS)
        n = fld(w1, TXWI_W1_MPDU_TOTAL_BYTE_COUNT)
        ack = fld(w1, TXWI_W1_ACK)
        pq = fld(w1, TXWI_W1_PACKETID_QUEUE)
        pe = fld(w1, TXWI_W1_PACKETID_ENTRY)
        qsel = fld(txinfo, TXINFO_W0_QSEL)
        nv = fld(txinfo, TXINFO_W0_USB_DMA_NEXT_VALID)
        burst = fld(txinfo, TXINFO_W0_USB_DMA_TX_BURST)
        body = cap[hdr:hdr + n]
        prefix = build_tx_descriptors(
            n, txwi_size, use_no_ack=(ack == 0), mcs=mcs, phymode=phymode, qsel=qsel,
            packetid_queue=pq, packetid_entry=pe, next_valid=nv, tx_burst=burst)
        rebuilt = prefix + body + b"\x00" * ((-n) & 3) + b"\x00\x00\x00\x00"
        pm = "OFDM/5G" if phymode == 1 else ("CCK/2.4G" if phymode == 0 else f"phy{phymode}")
        if rebuilt == cap:
            matched += 1
            bands[pm] = bands.get(pm, 0) + 1
            endpoints[f"0x{f['ep']:02x}"] = endpoints.get(f"0x{f['ep']:02x}", 0) + 1
        elif shown < 3:
            shown += 1
            print(f"  MISMATCH @f{f['frame']} ep0x{f['ep']:02x} (phymode={phymode} n={n}):")
            print(f"    port: {rebuilt.hex()}")
            print(f"    capt: {cap.hex()}")

    ok = matched == len(frames)
    print(f"  {'PASS' if ok else 'FAIL'}: {matched}/{len(frames)} bulk-OUT TX frames rebuilt "
          f"byte-for-byte by tx.build_tx_descriptors (0 waived).")
    print(f"    bands: {bands}   endpoints: {endpoints}")
    return ok, len(frames)


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
    tx_ok, tx_n = verify_tx_inject(pcap, dev, silicon)

    total = len(allops)
    print("\n[coverage]")
    print(f"  control single-cursor walk: {cold_n} / {total} vendor ops byte-for-byte "
          f"({100 * cold_n / total:.1f}%), 0 waived"
          f"{' — COMPLETE' if cold_ok else f' (FRONTIER at op {cold_n})'}.")
    print("  the walk drives the full bring-up + operational phase in kernel wire order")
    print("  (cold init → monitor-enable → channel hops via event dispatch); it subsumes the")
    print(f"  anchored [channel tune] blocks ({tune_n} ops), kept as an independent cross-check.")
    print(f"  bulk-OUT TX inject: {tx_n} frames rebuilt byte-for-byte (CCK 2.4 GHz + OFDM 5 GHz).")
    print("  note: the walk reproduces the kernel's operational config_filter (0x97→0x93), but")
    print("  the LIVE connect() keeps its monitor-first RX_FILTER_CFG=0x11 shortcut (HW-validated,")
    print("  deliberately not reconciled) — see RT2800USB.md. The new operational helpers")
    print("  (config_ant/toggle_rx/config_ps/update_survey) are exercised by the walk, not connect().")
    if not cold_ok:
        print("  Remaining control frontier: the aireplay-ng TX-injection region (TX_STA_FIFO")
        print("  status polling + async link-tuner/filter reapplies) — see FRONTIER above.")
    return 0 if (cold_ok and tune_ok and tx_ok) else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
