"""Acceptance gate: ONE monotonic single-cursor walk of the rtl8814au_dkms cold-boot capture.

Fail-closed, never short-circuited. One cursor runs from the driver's first vendor op
(the probe chip-version read) to the end of the capture; every op has exactly one honest
fate as the cursor advances:

  * matched       — the port's real handler reproduces it byte-for-byte at the cursor.
  * unaccounted   — anything else STOPS the walk and names the op: the porting frontier,
                    the next thing to make faithful. PASS <=> zero unaccounted.

This capture has **no aireplay-ng injection** (every bulk-OUT is a firmware-download
packet in frames 6145-6667), so there is **no legitimate waiver** — the whole control
conversation, init + airmon STA->monitor dance + airodump hops + the phydm dynamic-check
watchdog, is reproduce-or-fail to the last frame.

We do not run airmon/airodump/iw against our port; the chip only sees register writes, so
the vendor-driver writes those tools trigger are ours to reproduce. wifit3 is the trigger:
the bring-up + airmon dance is the deterministic init walk; the operational phase dispatches
each burst to the real handler at the cursor — a channel hop (set_channel_bw) or a
dynamic-check tick (the sreset poll + phydm watchdog) — distinguished by a unique opener.

    uv run python scripts/rtl8814au_dkms/verify_pcap.py [capture-N[.pcap]]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8814au_dkms import (  # noqa: E402
    bb, chan, dm, efuse, firmware, mac, monitor, rf, watchdog,
)
from wifit3.chips.rtl8814au_dkms import constants as C  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"
DEV_ADDR = {"capture-1": 51, "capture-2": 53, "capture-3": 54}

INIT_CHANNEL = 1                 # the cold-boot connect-time tune target
_ANCHOR_ADDR = C.REG_SYS_CFG1    # 0xF0 — read_chip_version, the first vendor op
# Operational-dispatch openers (each the unique first op of a vendor handler at the cursor):
_OP_HOP = C.REG_CCK_CHECK        # 0x0454 R: phy_SwBand8814A band-marker read opens a tune
_OP_LED = 0x0060                 # R: SwLedOn/Off_8814AU — the LED blink (a separate producer)
_OP_TICK = 0x0210                # R: rtl8814_sreset_xmit_status_check opens a dynamic-check tick
_REG_RF_CHNL_A = 0x0C90          # path-A RF_CHNLBW MMIO write — carries the tuned channel


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler at the cursor
    against the recorded wire; it advances by exactly the ops the handler consumed."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0

    def run(self, fn, label: str):
        t = rp.ReplayTransport(self.ops[self.i:])
        result = fn(t)
        self.i += t.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None


def _peek_channel(ops: list[dict], i: int, window: int = 120) -> int | None:
    """The channel a tune targets is a runtime input (airodump/iw choose it); read it from
    the wire's first path-A RF_CHNLBW write in the upcoming burst. RF reg 0x18[7:0] holds the
    channel number (1..165 all fit a byte; the band/sub-band bits sit higher)."""
    for o in ops[i:i + window]:
        if o["kind"] == "W" and o.get("addr") == _REG_RF_CHNL_A:
            return o["value"] & 0xFF
    return None


def _walk_init(w: Walk, fw: bytes) -> tuple[efuse.ChipParams, int]:
    """Probe + deterministic bring-up, in driver source order, threaded through one cursor.
    Mirrors driver.py connect(): EFUSE -> firmware -> MAC/BB/RF -> tune -> InitHalDm ->
    RFE-true -> turn-on tail. Returns (params, the DIG IGI seed the watchdog carries)."""
    p = w.run(efuse.read_chip_params, "efuse")                            # probe (pre power-on)
    w.run(lambda t: firmware.bring_up(t, fw), "bring-up")                 # M1: pwr-on -> FW ready
    w.run(mac.phy_mac_config, "mac-cfg")                                  # MAC register table
    w.run(mac.mac_init_misc, "mac-misc")                                  # hal_init MISC stage
    w.run(lambda t: bb.phy_bb_config(t, p.rfe_type, p.crystal_cap), "bb-cfg")
    w.run(lambda t: rf.phy_rf_config(t, p.rfe_type), "rf-cfg")
    w.run(lambda t: chan.init_tune(t, INIT_CHANNEL, p.tx_power, p.tx_power_5g,
                                   p.bb_swing, p.bb_swing_5g), "init-tune")
    igi_seed = w.run(dm.init_hal_dm, "init-hal-dm")                       # phydm DIG/AGC seed
    w.run(lambda t: chan.set_rfe_reg_init(t, p.rfe_type), "rfe-init")     # PHY_SetRFEReg(TRUE)
    w.run(lambda t: mac.hal_init_turn_on(t, p.mac_address), "turn-on")    # turn-on tail + MAC
    return p, igi_seed


def _walk_airmon(w: Walk, p: efuse.ChipParams) -> int:
    """The airmon STA->monitor dance — reproduced, not skipped. init_hw_mlme_ext's RX-BAR +
    channel re-tune, then hw_var_set_opmode(STATION) then hw_var_set_opmode(MONITOR).

    init_hw_mlme_ext resets the software band to BAND_MAX, so the ch1 retune (chip already
    2.4 GHz, no switch) skips CCK txagc. Returns the band the dance leaves committed."""
    w.run(monitor.enable_rx_bar, "rx-bar")                                # init_hw_mlme_ext
    band = w.run(lambda t: chan.set_channel_bw(t, INIT_CHANNEL, p.tx_power, p.tx_power_5g,
                                               p.bb_swing, p.bb_swing_5g,
                                               current_band=C.BAND_MAX), "retune")
    w.run(lambda t: monitor.set_sta_opmode(t, p.mac_address), "sta-opmode")
    w.run(monitor.enter_monitor, "monitor")
    return band


def _walk_operational(w: Walk, p: efuse.ChipParams, band: int,
                      igi_seed: int) -> tuple[int, int, int, dict | None]:
    """Dispatch each operational burst to the real vendor handler at the cursor. Three producers
    interleave: channel hops (carry the lagging software band), the LED blink (carries an ON/OFF
    phase), and the dynamic-check tick (sreset + phydm_watchdog, carrying DM state). The first op
    that opens no wired handler STOPS the walk and is returned as the frontier."""
    st = watchdog.WatchdogState(cur_ig_value=igi_seed)
    hops = ticks = leds = 0
    while w.i < len(w.ops):
        o = w.peek()
        if o["kind"] == "R" and o.get("addr") == _OP_HOP:
            ch = _peek_channel(w.ops, w.i)
            if ch is None:
                break
            try:
                band = w.run(lambda t, c=ch, b=band: chan.set_channel_bw(
                    t, c, p.tx_power, p.tx_power_5g, p.bb_swing, p.bb_swing_5g,
                    current_band=b), f"hop{ch}")
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, _frontier(w, o, f"hop ch{ch}", e)
            hops += 1
            continue
        if o["kind"] == "R" and o.get("addr") == _OP_LED:
            try:
                w.run(lambda t: watchdog.led_blink(t, st), f"led#{leds + 1}")
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, _frontier(w, o, f"led #{leds + 1}", e)
            leds += 1
            continue
        if o["kind"] == "R" and o.get("addr") == _OP_TICK:
            try:
                w.run(lambda t: watchdog.tick(t, st), f"tick#{ticks + 1}")
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, _frontier(w, o, f"tick #{ticks + 1}", e)
            ticks += 1
            continue
        break  # frontier: unknown opener
    return hops, ticks, leds, w.peek()


def _frontier(w: Walk, o: dict, label: str, e: Exception) -> dict:
    kind = type(e).__name__
    print(f"\n  {label} {'DIVERGED' if isinstance(e, rp.Divergence) else 'ERROR'} "
          f"at op {w.i}:\n    {kind}: {e}")
    return o


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = DEV_ADDR.get(name) or rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    fw = FW_BIN.read_bytes()

    full = rp.extract_ops(pcap, dev)
    anchor = next((j for j, o in enumerate(full)
                   if o["kind"] == "R" and o.get("addr") == _ANCHOR_ADDR and o["width"] == 4),
                  None)
    if anchor is None:
        print("FAIL: no REG_SYS_CFG1/4 read (read_chip_version) in capture")
        return 1
    ops = full[anchor:]
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev}, {len(full)} vendor ops -> walk {total} from op {anchor}")

    w = Walk(ops)
    try:
        p, igi_seed = _walk_init(w, fw)
        print(f"  init: reproduced {w.i} ops single-cursor (efuse -> turn-on tail, no gaps)")
        init_end = w.i
        band = _walk_airmon(w, p)
        print(f"  airmon: reproduced {w.i - init_end} ops (RX-BAR + retune + STA + monitor)")
    except rp.Divergence as e:
        print(f"\nFAIL (init/airmon divergence) at op {w.i}:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2

    hops, ticks, leds, frontier = _walk_operational(w, p, band, igi_seed)
    print(f"  operational: {hops} channel hops + {ticks} dynamic-check ticks + {leds} LED "
          f"blinks reproduced")

    if frontier is not None:
        fa = frontier
        desc = (f"{fa['kind']} 0x{fa.get('addr', 0):04x}/{fa.get('width', '?')}"
                f"=0x{fa.get('value', 0):x}" if fa["kind"] != "B" else "bulk")
        print(f"\nFRONTIER: reproduced {w.i} of {total} ops; first unaccounted op "
              f"@{w.i} = {desc} (frame {fa.get('frame')})")
        print("  ^ the next thing to make faithful (port it).")
        return 1

    print(f"\nPASS: reproduced all {w.i} of {total} ops single-cursor — entire cold-boot "
          f"capture byte-for-byte (init + airmon + {hops} hops + {ticks} watchdog ticks).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
