"""Acceptance gate: replay-diff the rtl8188eus_dkms port against its vendor cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed. This is the *method*, modelled on
``scripts/rtl8187/verify_pcap.py`` (single extract, single cursor, real-function calls,
explicit named boundaries) and extended with the operational-phase **dispatch** the async
phydm chips need. Every op the card emitted has exactly one honest fate:

  * **matched**   — the port's real handler reproduces it byte-for-byte at the cursor.
  * **waived**    — an explicit, *named*, *counted* boundary for a producer that is not the
                    vendor driver: aireplay-ng's injected TX (a different userland program;
                    bulk-OUT frames + its RA TX-report-timing writes). A waiver is reported,
                    never a silent strip.
  * **unaccounted** — anything else STOPS the walk and names the op. That is the porting
                    frontier: the next thing to make faithful. PASS ⇔ zero unaccounted.

We do not run airmon-ng / airodump-ng / iw / aireplay-ng against our port; the chip only
sees register writes, so the *vendor-driver* writes those tools trigger are ours to
reproduce. wifit3 itself is the trigger: ``connect()`` stands in for airmon (monitor entry),
the channel hopper for airodump/iw (per-hop ``set_channel``), and the 2 s ``dig`` task for
``rtw_dynamic_chk_wk_hdl`` (the sreset poll + phydm watchdog). So the operational dispatch
runs those same real handlers at the cursor — monitor RX-BAR/opmode, channel tunes, and the
dynamic-check tick — carrying DM/channel state across fires.

    uv run python scripts/rtl8188eus_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus_dkms import (  # noqa: E402
    bb, chan, dig, dm, efuse, firmware, mac, monitor, powertrack, pwrseq, rf, sreset,
    txpower,
)
from wifit3.chips.rtl8188eus_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8188eus_dkms.constants import DEFAULT_INIT_CHANNEL  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8188eu" / "capture-1.pcap"

REG_APS_FSMCO_B2 = 0x0006   # first power-seq op (CARDEMU_TO_ACT step-1 poll) — the init anchor
REG_SYS_CFG = 0x00F0        # 32-bit SYS_CFG read; used to anchor the walk (see _is_syscfg_read)
REG_FA_HOLD = 0x0C00        # phydm FA-counter hold — opens the watchdog slot of a tick
EEPROM_THERMAL_METER_88E = 0xBA   # efuse offset of the thermal-meter base
EEPROM_DEFAULT_THERMAL_88E = 0x18  # fallback when the efuse byte is 0xff (autoload fail)

# Operational-dispatch opener signatures (each a unique first op of a vendor handler):
_OP_RX_BAR = C.REG_RXFLTMAP1            # 0x06a2 R: init_hw_mlme_ext HW_VAR_ENABLE_RX_BAR
_OP_MONITOR = C.MSR                     # 0x0102 R: hw_var_set_opmode(MONITOR) opens on MSR
_OP_CHAN = C.rTxAGC_A_CCK1_Mcs32        # 0x0e08 R: set_channel's set_tx_power CCK1 RMW
_OP_TICK = C.REG_TXDMA_STATUS           # 0x0210 R: rtw_dynamic_chk_wk_hdl sreset xmit check
_RF_CHNL_LOW10 = 0x3FF                  # RfRegChnlVal channel field [9:0]
_REG_TX_RPT_TIME = 0x04F0               # RA TX-report timing — written only on TX (aireplay)


def _is_syscfg_read(o: dict) -> bool:
    """A 32-bit ``R REG_SYS_CFG``. Used only to anchor the walk at the first such read —
    ``read_chip_version`` at probe. (All 24 in the capture are identified + reproduced now:
    chip-version, the TX-power-track foundry read at the RF-config tail, and the per-tick
    phydm_receiver_blocking read; none are waived.)"""
    return o["kind"] == "R" and o.get("addr") == REG_SYS_CFG and o["width"] == 4


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler at the cursor;
    ``waive`` consumes one op of a named non-reproduced producer; both advance the cursor."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn, label: str):
        """Replay ``fn`` (a real bring-up/handler call) against the wire from the cursor."""
        t = rp.ReplayTransport(self.ops[self.i:])
        result = fn(t)
        consumed = t.i
        self.i += consumed
        return result, consumed

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _seed_dm_state(dm_seed, params):
    """Build the carried DM state from the seed InitHalDm returned — running the *real*
    seeding handlers (dig.seed_state / powertrack.seed_state), the same ones the driver's
    _dig_watchdog uses. No modeling: the gate exercises the actual code path, and because the
    seed is carried (not re-read), the watchdog start emits no extra wire ops."""
    raw = params.efuse_map[EEPROM_THERMAL_METER_88E]
    eeprom_thermal = EEPROM_DEFAULT_THERMAL_88E if raw == 0xFF else raw
    state = dig.seed_state(dm_seed.igi, dm_seed.cck_cca)
    pt = powertrack.seed_state(dm_seed.ofdm_swing_raw, dm_seed.cck_swing_raw, eeprom_thermal)
    return state, pt


def _walk_init(w: Walk, out: dict) -> None:
    """The deterministic bring-up, in driver source order, threaded through one cursor.
    No re-anchoring: each call consumes the next span of the same wire. Mirrors
    driver.py connect() + _phy_config()."""
    w.run(lambda t: efuse.read_adapter_info(t), "adapter-info")   # probe (pre power-on)
    w.run(lambda t: pwrseq.power_on(t), "power-on")
    params, _ = w.run(lambda t: efuse.read_chip_params(t), "efuse")
    out["params"] = params
    w.run(lambda t: mac.init_misc01(t), "misc01")
    w.run(lambda t: firmware.download_firmware(t, firmware.load_firmware_blob()), "firmware")
    w.run(lambda t: mac.phy_mac_config(t), "mac-cfg")
    w.run(lambda t: bb.phy_bb_config(t, crystal_cap=params.crystal_cap), "bb-cfg")
    w.run(lambda t: rf.phy_rf_config(t), "rf-cfg")
    w.run(lambda t: efuse.iol_efuse_patch(t), "efuse-patch")
    w.run(lambda t: mac.init_tx_buffer_boundary(t), "tx-buf")
    w.run(lambda t: mac.init_llt(t), "llt")
    w.run(lambda t: mac.init_misc02(t), "misc02")
    (rf_chnl_pair, _) = w.run(lambda t: rf.read_rf_chnl_val(t), "rf-chnl")
    out["rf_chnl"] = rf_chnl_pair[0]                          # RfRegChnlVal[A], tune base
    w.run(lambda t: bb.bb_turn_on_block(t), "bb-on")
    w.run(lambda t: mac.invalidate_cam_all(t), "cam-clear")
    w.run(lambda t: txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL), "tx-power")
    w.run(lambda t: mac.init_misc11_tail(t), "misc11")
    (dm_seed, _) = w.run(lambda t: dm.init_hal_dm(t), "init-hal-dm")
    out["dm_seed"] = dm_seed                                  # carried into the watchdog
    w.run(lambda t: dm.init_hal_tail(t), "hal-tail")
    w.run(lambda t: mac.set_macid(t, params.mac_address or b"\x00" * 6), "set-macid")


def _peek_channel(ops: list[dict], i: int, window: int = 80) -> int | None:
    """The channel a tune targets is a runtime input (airodump/iw choose it); read it from
    the wire's first RF_CHNLBW (RF reg 0x18) write in the upcoming burst, like the watchdog
    reads its FA counters. The LSSI write packs (addr<<20)|data; channel = data[9:0]."""
    for o in ops[i:i + window]:
        if (o["kind"] == "W" and o.get("addr") == C.RF_LSSI_WRITE_A
                and (o["value"] >> 20) == C.RF_CHNLBW):
            return o["value"] & _RF_CHNL_LOW10
    return None


def _walk_operational(w: Walk, params, rf_chnl: int, state, pt) -> tuple[list[int], dict | None]:
    """Dispatch each operational burst to the real vendor handler at the cursor, carrying
    channel + DM state across fires. The first op that is neither a wired handler nor a named
    waiver STOPS the walk and is returned as the frontier."""
    ticks: list[int] = []
    while w.i < len(w.ops):
        o = w.peek()

        # aireplay-ng's injected frames (a different userland program's TX) — bulk-OUT. The
        # injection-test + deauth window is the capture tail (pcap_slicer: cap1 frames
        # 20622-25054); we port the vendor driver, not aireplay, so its frames are waived.
        if o["kind"] == "B":
            w.waive("aireplay-ng injection-test + deauth TX (external tool; bulk-OUT)")
            continue

        addr = o.get("addr")

        # aireplay's injection makes the firmware emit TX reports, so the RA TX-report-timing
        # write (REG_TX_RPT_TIME) interleaves the injected frames. It is TX-driven (the no-link
        # RX ticks never touch it) [SRC] hal8188erateadaptive.c:1206 odm_ra_set_tx_rpt_time —
        # the same external-tool activity as the bulk frames.
        if o["kind"] == "W" and addr == _REG_TX_RPT_TIME:
            w.waive("aireplay-triggered RA TX-report timing (REG_TX_RPT_TIME) "
                    "[external tool; hal8188erateadaptive.c]")
            continue

        # Dynamic-check tick (rtw_dynamic_chk_wk_hdl, 2 s): silent-reset status poll then the
        # phydm watchdog, one IO-locked burst. Both carry no/own state; DM state is threaded.
        if o["kind"] == "R" and addr == _OP_TICK:
            try:
                _, c1 = w.run(lambda t: sreset.status_check(t), "sreset")
                _, c2 = w.run(lambda t: dig.watchdog_tick(t, state, pt), "wd-tick")
            except Exception as e:  # noqa: BLE001
                return ticks, _frontier(w, o, f"tick #{len(ticks) + 1}", e)
            ticks.append(c1 + c2)
            continue

        # A bare watchdog with no preceding sreset poll (defensive — not seen in cap1).
        if o["kind"] == "R" and addr == REG_FA_HOLD:
            try:
                _, c = w.run(lambda t: dig.watchdog_tick(t, state, pt), "wd-tick")
            except Exception as e:  # noqa: BLE001
                return ticks, _frontier(w, o, f"tick #{len(ticks) + 1}", e)
            ticks.append(c)
            continue

        # init_hw_mlme_ext: enable RX-BAR (RXFLTMAP1 |= BIT8).
        if o["kind"] == "R" and addr == _OP_RX_BAR:
            try:
                w.run(lambda t: monitor.enable_rx_bar(t), "rx-bar")
            except Exception as e:  # noqa: BLE001
                return ticks, _frontier(w, o, "rx-bar", e)
            continue

        # hw_var_set_opmode(MONITOR): MSR(NOLINK) + RCR + RXFLTMAP2.
        if o["kind"] == "R" and addr == _OP_MONITOR:
            try:
                w.run(lambda t: monitor.enter_monitor(t), "monitor")
            except Exception as e:  # noqa: BLE001
                return ticks, _frontier(w, o, "monitor", e)
            continue

        # A channel tune (init_hw_mlme_ext's set_channel, or an airodump/iw hop). Channel is
        # the runtime input — peeked from the wire's RF_CHNLBW write.
        if o["kind"] == "R" and addr == _OP_CHAN:
            channel = _peek_channel(w.ops, w.i)
            if channel is None:
                break
            try:
                rf_chnl, _ = w.run(
                    lambda t, ch=channel, rc=rf_chnl:
                        chan.set_channel(t, params.tx_power, rc, ch), f"chan{channel}")
            except Exception as e:  # noqa: BLE001
                return ticks, _frontier(w, o, f"chan{channel}", e)
            continue

        break  # frontier: unknown opener
    return ticks, w.peek()


def _frontier(w: Walk, o: dict, label: str, e: Exception) -> dict:
    kind = type(e).__name__
    print(f"\n  {label} {'DIVERGED' if isinstance(e, rp.Divergence) else 'ERROR'} "
          f"at op {w.i}:\n    {kind}: {e}")
    return o


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)

    # ONE op list, anchored at the very first vendor op: read_chip_version's REG_SYS_CFG read
    # (the head of the probe prologue, now reproduced by efuse.read_adapter_info).
    full = rp.extract_ops(pcap, dev)
    anchor = next((i for i, o in enumerate(full) if _is_syscfg_read(o)), None)
    if anchor is None:
        print("FAIL: no REG_SYS_CFG/4 read (read_chip_version) in capture")
        return 1

    # NO filtering: every R 0xF0/4 is now an identified, reproduced op — read_chip_version at
    # probe (read_adapter_info), the TX-power-track foundry read at the RF-config tail
    # (rf.phy_rf_config), and phydm_receiver_blocking at the head of each watchdog tick.
    ops = full[anchor:]
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev}, {len(full)} vendor ops -> walk {total} ops")

    w = Walk(ops)
    out: dict = {}

    # 1) INIT — single cursor, source order.
    try:
        _walk_init(w, out)
    except rp.Divergence as e:
        print(f"\nFAIL (init divergence) at op {w.i}:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2
    params = out["params"]
    init_end = w.i
    print(f"  init: reproduced {init_end} ops single-cursor (power-on -> set-macid, no gaps)")

    # 2) OPERATIONAL — dispatch each burst to a real handler, carrying state. NOTHING beyond
    #    the named waivers is pre-allowed: the first op that is neither a wired handler nor a
    #    waiver STOPS the walk and is reported as the frontier.
    state, pt = _seed_dm_state(out["dm_seed"], params)
    ticks, frontier = _walk_operational(w, params, out["rf_chnl"], state, pt)

    # 3) Report — fail-closed. PASS only if the whole stream is matched-or-waived.
    print(f"  operational: dispatched {len(ticks)} dynamic-check ticks "
          f"(sreset poll + phydm watchdog, {sum(ticks)} ops byte-faithful, carried state)")
    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")

    if frontier is not None:
        fa = frontier
        desc = (f"{fa['kind']} 0x{fa.get('addr', 0):04x}/{fa.get('width', '?')}"
                f"=0x{fa.get('value', 0):x}" if fa["kind"] != "B" else "bulk")
        print(f"\nFRONTIER: reproduced {w.i} of {total} ops; first unaccounted op "
              f"@{w.i} = {desc} (frame {fa.get('frame')})")
        print("  ^ the next thing to make faithful (port it, or add a named waiver).")
        return 1

    print(f"\nPASS: reproduced {w.i} of {total} ops — every op matched or explicitly waived.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
