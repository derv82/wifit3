"""Acceptance gate: replay-diff the clean-room rt5370 (RT5390) port against its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed. Modelled on ``scripts/chips/rt5372/verify_pcap.py``
(the 2T2R sibling) and ``scripts/chips/rt3070/verify_pcap.py``: the replay is at the
``ctrl_transfer`` layer (``rt2x00_pcap_replay.ReplayDevice``) and the REAL ``chips/rt5370``
transport drives it, so every helper (regbusy poll, write_multi chunking, the EFUSE walker)
replays with zero reimplementation.

  * **matched**     — the port's real handler reproduces the op byte-for-byte at the cursor.
  * **waived**      — an explicit, *named*, *counted* boundary for a producer that is NOT the
                      rt2800usb kernel driver (aireplay-ng's TX_STA_FIFO status polling from the
                      human-fired injection; bulk-OUT TX itself never enters the control stream).
  * **unaccounted** — anything else STOPS the walk and names the op. That op IS the porting
                      frontier: the next op to reproduce. PASS ⇔ zero unaccounted.

RULES (do not violate — this is the whole point of the gate):
  * NEVER edit this file to make it print PASS.
  * NEVER copy logic from chips/rt2800usb/ — it is the imitation port (confirmed EFUSE
    word-vs-byte bug) that the clean-room drivers replace. Port from the kernel C in
    driver_sources/rt2x00-source-v6.18/ and let THIS wire confirm it. The rt5372 sibling's
    init_bbp_53xx / config_channel_rf53xx are the SAME shared kernel functions, but every
    RT5390-specific value (init_rfcsr_5390, the rev-F RFCSR55/59 channel tables) is ported
    fresh from the C — a green walk is necessary, not sufficient.
  * The cursor only advances by reproducing the wire or by an explicit named waiver.

    uv run python scripts/porting/verify_pcap.py rt5370 [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rt2x00_pcap_replay as rp  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rt2800usb_rt5370"
MAC_CSR0 = 0x1000          # silicon id + revision; the first vendor op of the probe

_IMPORT_ERR = None
try:
    from wifit3.chips.rt5370.transport import RT5370Transport  # noqa: E402
    from wifit3.chips.rt5370 import (  # noqa: E402
        bbp, chan, constants as C, eeprom, firmware, mac, monitor, rfcsr)
except ImportError as e:  # driver not scaffolded yet
    _IMPORT_ERR = e


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler against the wire
    from the cursor (a fresh ReplayDevice over the remaining ops, wrapped in the real chip
    transport); ``waive`` consumes one op of a named non-reproduced producer. Both advance."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn, label: str):
        rd = rp.ReplayDevice(self.ops[self.i:])
        t = RT5370Transport(rd)
        result = fn(t)
        self.i += rd.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _walk_init(w: Walk, out: dict) -> None:
    """Deterministic cold bring-up, one cursor, no re-anchoring. Mirrors driver.py
    _bringup(). WIRE ORDER (rt2x00 family): probe → EFUSE → gpio → firmware → enable_radio.

    Source: driver_sources/rt2x00-source-v6.18/{rt2800usb.c,rt2800lib.c,rt2x00dev.c}.

    probe_rt     read MAC_CSR0 (chip id/rev = 0x5390)  rt2800_probe_rt (11987)
    efuse        autorun + EFUSE 32 word-blocks         rt2800usb_read_eeprom (594) ->
                 (WORD offset!)                         rt2800_read_eeprom_efuse (10955)
    gpio-rfkill  GPIO_CTRL dir (last probe-hw op)       rt2800_probe_hw (12057)
    firmware     FW upload + MCU boot signal            rt2800_load_firmware (714) ->
                                                        rt2800usb_write_firmware (210)
    --- M2+ frontier: enable_radio / init_registers / BBP / RFCSR (5390) / channel ---
    """
    chip = w.run(lambda t: mac.probe_rt(t), "probe-rt")              # MAC_CSR0
    out["chip"] = chip
    buf = w.run(lambda t: eeprom.read_eeprom_efuse(t), "efuse")      # autorun + EFUSE
    buf = eeprom.validate_eeprom(buf)              # blank/invalid-field fix-up (no-op here)
    out["eeprom"] = eeprom.parse_eeprom(buf)
    ev = out["eeprom"]
    w.run(lambda t: mac.probe_hw_gpio(t), "gpio-rfkill")             # GPIO_CTRL dir
    fw = firmware.load_firmware_blob()
    w.run(lambda t: firmware.upload(t, fw), "firmware")              # FW load + boot

    # rt2x00lib_enable_radio -> set_device_state(RADIO_ON) -> rt2800usb_enable_radio
    w.run(lambda t: mac.set_radio_led(t, ev), "radio-led")           # leds-class radio on
    w.run(lambda t: mac.wakeup(t), "wakeup")                         # MCU_WAKEUP (AWAKE)
    w.run(lambda t: mac.usb_enable_radio_dma(t), "usb-dma")          # wait_wpdma + USB_DMA_CFG
    w.run(lambda t: t.wait_wpdma_ready(), "enable-radio-wpdma")      # rt2800_enable_radio prologue
    w.run(lambda t: mac.init_registers(t, chip, ev), "init-registers")  # MAC config block
    w.run(lambda t: mac.enable_radio_boot(t), "enable-radio-boot")   # BBP/RF ready + boot signal
    w.run(lambda t: bbp.init_bbp(t, chip, ev), "init-bbp")           # BBP regs (53xx + EEPROM)
    out["drv"] = w.run(lambda t: rfcsr.init_rfcsr_5390(t, chip, ev), "init-rfcsr")  # RFCSR (no cal)
    w.run(lambda t: mac.enable_radio_finish(t, chip, ev), "enable-radio-finish")  # RX/LED (no MCU_CURRENT)
    w.run(lambda t: mac.set_radio_led(t, ev), "radio-led-on")        # rt2x00leds_led_radio(true)
    w.run(lambda t: mac.start_queue_rx(t), "start-queue-rx")         # rt2x00queue_start_queues


def _peek_channel(ops: list[dict], i: int) -> int | None:
    """Reverse-map the next config_channel_rf53xx tune to a channel by peeking its
    RFCSR8 (rf1) + RFCSR9 (rf3) writes [SRC RF_VALS_3X_2G + rt2800lib.c:3395-3399].
    Returns None if no RFCSR channel tune (rfcsr8 write) appears in the look-ahead
    window — i.e. the upcoming block is not a channel hop."""
    rf1 = rf3 = None
    for o in ops[i:i + 60]:
        if o["dir"] != "OUT" or o["addr"] != C.RF_CSR_CFG or not o.get("data"):
            continue
        v = int.from_bytes(o["data"], "little")
        if not (v & C.RF_CSR_CFG_WRITE):          # skip read-initiating writes
            continue
        regnum, data = (v >> 8) & 0x3F, v & 0xFF
        if regnum == 8 and rf1 is None:
            rf1 = data
        elif regnum == 9 and rf3 is None:
            rf3 = data
        if rf1 is not None and rf3 is not None:
            break
    if rf1 is None:
        return None
    for ch, (a, _b, c) in C.RF_VALS_3X_2G.items():
        if a == rf1 and c == rf3:
            return ch
    raise rp.Divergence(f"unknown RF53xx tune rf1={rf1} rf3={rf3} at op{i}")


_AIREPLAY_TAIL = ("aireplay --test + -0 deauth: TX_STA_FIFO polling "
                  "(human-fired TX; bulk-OUT out of the control gate)")


def _walk_operational(w: Walk, chip, ev, drv, out: dict) -> dict | None:
    # airmon-ng start: interface-up filter (0x97) → initial config → monitor filter (0x93).
    w.run(lambda t: monitor.enable_monitor(t, chip, ev, drv), "enable-monitor")

    while w.i < len(w.ops):
        o = w.peek()
        # airodump / iw channel hop: a full rt2x00mac_config(CHANGE_CHANNEL).
        if o["dir"] == "IN" and o["addr"] == C.MAC_SYS_CTRL:
            ch = _peek_channel(w.ops, w.i)
            if ch is not None:
                w.run(lambda t, ch=ch: chan.set_channel(t, chip, ev, drv, ch), f"chan{ch}")
                continue
        # mac80211 re-applies the monitor filter when a tool opens its socket (0x93).
        if o["dir"] == "IN" and o["addr"] == C.RX_FILTER_CFG:
            w.run(lambda t: mac.config_filter(t, monitor.MONITOR_FILTER, monitoring=True),
                  "filter-reapply")
            continue
        # aireplay TX-status polling — the one named, counted waiver.
        if o["addr"] == C.TX_STA_FIFO:
            w.waive(_AIREPLAY_TAIL)
            continue
        break
    return w.peek()


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    full = rp.extract_ops(pcap, dev)
    csr0 = next((o for o in full if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    silicon = (int.from_bytes(csr0["data"], "little") >> 16) & 0xFFFF if csr0 else 0
    rev = int.from_bytes(csr0["data"], "little") & 0xFFFF if csr0 else 0

    # Anchor at the first vendor op (the MAC_CSR0 silicon/rev read at probe).
    anchor = next((i for i, o in enumerate(full) if o is csr0), 0)
    ops = full[anchor:]
    print(f"{name}: card=dev{dev}, {len(full)} vendor ops -> walk {len(ops)} "
          f"(silicon=0x{silicon:04x} rev=0x{rev:04x})")

    if _IMPORT_ERR is not None:
        print(f"\nrt5370 driver not scaffolded yet ({_IMPORT_ERR}).")
        first = ops[0] if ops else None
        if first:
            print(f"FRONTIER: op 0 = {rp.ReplayDevice._fmt(first)}")
        print("  ^ start at M1 (probe + EFUSE). Build chips/rt5370/{transport,eeprom,...}.py "
              "from the kernel C; see chips/rt5370/RT5370.md. Re-run after each milestone.")
        return 1

    w = Walk(ops)
    out: dict = {}
    try:
        _walk_init(w, out)
    except rp.Divergence as e:
        print(f"\nFAIL (init divergence) at op {w.i}:\n  {e}")
        return 1
    except (AttributeError, NotImplementedError) as e:
        fr = w.peek()
        print(f"\nFRONTIER at op {w.i}: handler not ported yet ({type(e).__name__}: {e})")
        if fr:
            print(f"  next wire op = {rp.ReplayDevice._fmt(fr)}")
        return 1
    init_end = w.i
    print(f"  init: reproduced {init_end} ops single-cursor (probe -> chan-ready, no gaps)")

    try:
        frontier = _walk_operational(w, out.get("chip"), out.get("eeprom"), out.get("drv"), out)
    except rp.Divergence as e:
        print(f"\nFAIL (operational divergence) at op {w.i}:\n  {e}")
        return 1
    except (AttributeError, NotImplementedError) as e:
        fr = w.peek()
        print(f"\nFRONTIER at op {w.i}: handler not ported yet ({type(e).__name__}: {e})")
        if fr:
            print(f"  next wire op = {rp.ReplayDevice._fmt(fr)}")
        return 1
    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    if frontier is not None:
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.ReplayDevice._fmt(frontier)} (frame {frontier.get('frame')})")
        print("  ^ the next op to reproduce (port it, or add a named waiver).")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
