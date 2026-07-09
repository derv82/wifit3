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
        if hops:
            print(f"  OK    {len(hops)} channel hops (strict bracket, "
                  f"{n_filter} async filter reapplies) +{w.i - hop_start} ops  "
                  f"(cursor {anchor + w.i})")
            print(f"        channels covered: {sorted(set(hops))}")
    except rp.Divergence as e:
        fr = w.ops[w.i] if w.i < len(w.ops) else None
        print(f"  STOP  (diverged)\n        {e}")
        print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
              f"{' = ' + rp.ReplayDevice._fmt(fr) if fr else ''}.")
        return False, w.i, box["ev"]
    # Cold bring-up + monitor-enable + the channel-hop stream all reproduced in one
    # cursor. The next op is the porting frontier — the aireplay-ng TX-injection region.
    fr = w.ops[w.i] if w.i < len(w.ops) else None
    print(f"  reproduced {w.i} ops single-cursor byte-for-byte (0 waived) — the full cold"
          " bring-up (probe → EFUSE → firmware → radio-on → init_registers → BBP → RFCSR →"
          " enable_radio) + operational phase (monitor-enable + channel hops).")
    print(f"  FRONTIER at op {anchor + w.i}: next kernel op to port"
          f"{' = ' + rp.ReplayDevice._fmt(fr) if fr else ''} (aireplay-ng TX-injection"
          " region: bulk-OUT frames + TX_STA_FIFO polling).")
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

    total = len(allops)
    print("\n[coverage]")
    print(f"  single-cursor walk: {cold_n} / {total} vendor ops verified byte-for-byte "
          f"({100 * cold_n / total:.1f}%), 0 waived"
          f"{' — COMPLETE' if cold_ok else f' (FRONTIER at op {cold_n})'}.")
    print("  the walk drives the full bring-up + operational phase in kernel wire order")
    print("  (cold init → monitor-enable → channel hops via event dispatch); it subsumes the")
    print(f"  anchored [channel tune] blocks ({tune_n} ops), kept as an independent cross-check.")
    if not cold_ok:
        print("  Remaining frontier: the aireplay-ng TX-injection region (bulk-OUT frames +")
        print("  TX_STA_FIFO status polling + async link-tuner/filter reapplies) — see FRONTIER above.")
    return 0 if (cold_ok and tune_ok) else 1


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
