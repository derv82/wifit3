"""Acceptance gate: replay-diff the rtl8188eus_dkms port against its vendor cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed. This is the *method*, modelled on
``scripts/rtl8187/verify_pcap.py`` (single extract, single cursor, real-function calls,
explicit named boundaries) and extended with the operational-phase **dispatch** the async
phydm chips need. Every op the card emitted has exactly one honest fate:

  * **matched**   — the port's real handler reproduces it byte-for-byte at the cursor.
  * **waived**    — an explicit, *named*, *counted* boundary for a producer the port does
                    not reproduce by design (the async sreset timer; airmon's monitor
                    runtime). A waiver is reported, never a silent strip.
  * **unaccounted** — anything else STOPS the walk and names the op. That is the porting
                    frontier: the next thing to make faithful. PASS ⇔ zero unaccounted.

There is no per-milestone re-anchoring and no isolated seed-replay. ``reproduced N of M``
is the whole story; where it stops is where the port and the Linux driver first differ —
possibly in code we think is right, possibly in code not yet written.

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

import rtw88_pcap_replay as rp  # noqa: E402  (the shared family primitive: extract + replay)
from wifit3.chips.rtl8188eus_dkms import (  # noqa: E402
    bb, dig, dm, efuse, firmware, mac, powertrack, pwrseq, rf, txpower,
)
from wifit3.chips.rtl8188eus_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8188eus_dkms import powertrack_tbl as PT  # noqa: E402
from wifit3.chips.rtl8188eus_dkms.constants import DEFAULT_INIT_CHANNEL  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8188eu" / "capture-1.pcap"

REG_APS_FSMCO_B2 = 0x0006   # first power-seq op (CARDEMU_TO_ACT step-1 poll) — the init anchor
REG_SYS_CFG = 0x00F0        # the async sreset timer reads this 32-bit (see _is_sreset_poll)
REG_FA_HOLD = 0x0C00        # phydm FA-counter hold — opens every watchdog tick
EEPROM_THERMAL_METER_88E = 0xBA   # efuse offset of the thermal-meter base
EEPROM_DEFAULT_THERMAL_88E = 0x18  # fallback when the efuse byte is 0xff (autoload fail)

def _is_sreset_poll(o: dict) -> bool:
    """The silent-reset timer polls REG_SYS_CFG as a 32-bit read every ~2 s, interleaving
    into the EP0 stream from frame ~2731. It is a separate producer from the bring-up (which
    only ever reads SYS_CFG as bytes) and from phydm; our driver's sreset timer is not yet
    ported, so every such poll is a counted waiver."""
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


def _seed_dm_state(init_ops: list[dict], params):
    """Seed the carried DM state from the values InitHalDm left on the chip — the same seeds
    the live driver reads at watchdog start (IGI 0x20; CCK CCA default from 0xa08[23:16]; the
    thermal swing bases from 0xc80/0xa22; the efuse thermal base). Read from the init wire so
    the operational ticks start where the chip really was."""
    a0a = next(((o["value"] >> 16) & 0xFF for o in init_ops
                if o["kind"] == "R" and o.get("addr") == 0x0A08), 0)
    state = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=a0a)

    c80 = next((o["value"] for o in init_ops
                if o["kind"] == "R" and o.get("addr") == 0x0C80 and o["width"] == 4), 0)
    a22 = next((o["value"] for o in init_ops
                if o["kind"] == "R" and o.get("addr") == 0x0A22 and o["width"] == 1), 0)
    raw = params.efuse_map[EEPROM_THERMAL_METER_88E]
    eeprom_thermal = EEPROM_DEFAULT_THERMAL_88E if raw == 0xFF else raw
    ofdm_i = powertrack.get_swing_index(c80)
    cck_i = powertrack.get_cck_swing_index(a22)
    pt = powertrack.PowerTrackState(
        eeprom_thermal=eeprom_thermal,
        default_ofdm_index=ofdm_i if ofdm_i < len(PT.OFDM_SWING_TABLE) else 30,
        default_cck_index=cck_i if cck_i < len(PT.CCK_SWING_TABLE_CH1_CH13) else 20,
    )
    return state, pt


def _walk_init(w: Walk, params_box: list) -> None:
    """The deterministic bring-up, in driver source order, threaded through one cursor.
    No re-anchoring: each call consumes the next span of the same wire. Mirrors
    driver.py connect() + _phy_config()."""
    w.run(lambda t: pwrseq.power_on(t), "power-on")
    params, _ = w.run(lambda t: efuse.read_chip_params(t), "efuse")
    params_box.append(params)
    w.run(lambda t: mac.init_misc01(t), "misc01")
    w.run(lambda t: firmware.download_firmware(t, firmware.load_firmware_blob()), "firmware")
    w.run(lambda t: mac.phy_mac_config(t), "mac-cfg")
    w.run(lambda t: bb.phy_bb_config(t, crystal_cap=params.crystal_cap), "bb-cfg")
    w.run(lambda t: rf.phy_rf_config(t), "rf-cfg")
    w.run(lambda t: efuse.iol_efuse_patch(t), "efuse-patch")
    w.run(lambda t: mac.init_tx_buffer_boundary(t), "tx-buf")
    w.run(lambda t: mac.init_llt(t), "llt")
    w.run(lambda t: mac.init_misc02(t), "misc02")
    w.run(lambda t: rf.read_rf_chnl_val(t), "rf-chnl")
    w.run(lambda t: bb.bb_turn_on_block(t), "bb-on")
    w.run(lambda t: mac.invalidate_cam_all(t), "cam-clear")
    w.run(lambda t: txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL), "tx-power")
    w.run(lambda t: mac.init_misc11_tail(t), "misc11")
    w.run(lambda t: dm.init_hal_dm(t), "init-hal-dm")
    w.run(lambda t: dm.init_hal_tail(t), "hal-tail")
    w.run(lambda t: mac.set_macid(t, params.mac_address or b"\x00" * 6), "set-macid")


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)

    # ONE op list, anchored at the first power-seq op (the [0..5] chip-version probe ahead of
    # it is a read-only prologue we don't reproduce — a named, counted waiver).
    full = rp.extract_ops(pcap, dev)
    anchor = next((i for i, o in enumerate(full)
                   if o.get("addr") == REG_APS_FSMCO_B2), None)
    if anchor is None:
        print(f"FAIL: power-on anchor (0x{REG_APS_FSMCO_B2:04x}) not in capture")
        return 1
    waived_prologue = anchor

    # Filter the async sreset poll out of the cursor stream (counted, named — not a strip).
    from_anchor = full[anchor:]
    sreset = sum(1 for o in from_anchor if _is_sreset_poll(o))
    ops = [o for o in from_anchor if not _is_sreset_poll(o)]
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev}, {len(full)} vendor ops "
          f"({waived_prologue} prologue + {sreset} sreset-poll waived) -> walk {total} ops")

    w = Walk(ops)
    params_box: list = []

    # 1) INIT — single cursor, source order.
    try:
        _walk_init(w, params_box)
    except rp.Divergence as e:
        print(f"\nFAIL (init divergence) at op {w.i}:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2
    params = params_box[0]
    init_end = w.i
    print(f"  init: reproduced {init_end} ops single-cursor (power-on -> set-macid, no gaps)")

    # 2) OPERATIONAL — dispatch each burst to a real handler. Watchdog ticks (FA-hold 0xC00)
    #    replay the carried DM. NOTHING is pre-waived: the first op that is neither a tick nor
    #    a wired handler STOPS the walk and is reported, so we classify *that* op against the
    #    source — reproduce it (channel tune, monitor entry) or waive it with a citation.
    state, pt = _seed_dm_state(ops[:init_end], params)
    ticks: list[int] = []
    frontier: dict | None = None
    while w.i < len(ops):
        o = w.peek()
        if o["kind"] == "W" and o.get("addr") == REG_FA_HOLD:
            try:
                _, consumed = w.run(lambda t: dig.watchdog_tick(t, state, pt), "wd-tick")
                ticks.append(consumed)
            except rp.Divergence as e:
                print(f"\n  watchdog tick #{len(ticks) + 1} DIVERGED at op {w.i}:\n    {e}")
                frontier = o
                break
            except Exception as e:  # noqa: BLE001
                print(f"\n  watchdog tick #{len(ticks) + 1} ERROR at op {w.i}: "
                      f"{type(e).__name__}: {e}")
                frontier = o
                break
        else:
            frontier = o
            break

    # 3) Report — fail-closed. PASS only if the whole stream is matched-or-waived.
    print(f"  operational: dispatched {len(ticks)} watchdog ticks "
          f"({sum(ticks)} ops byte-faithful, carried state)")
    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    print(f"  prologue {waived_prologue:4} ops  — [0..5] chip-version probe (read-only)")
    print(f"  sreset   {sreset:4} ops  — async silent-reset timer poll (R 0xF0/4)")

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
