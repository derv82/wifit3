"""Byte-for-byte replay-diff of the rtl8188eus_dkms port against a vendor cold-boot capture.

PASS means ONLY: for this captured boot, the port emits the same USB bytes the vendor
driver did. It is a faithfulness gate, not a correctness proof (the only real proof is
beacons off the antenna). Fully offline -- no hardware.

============================ COVERAGE & BLIND SPOTS (read me) ============================
A green run here proves the INIT is byte-faithful. It does NOT prove runtime RX parity --
and the gap that fact hides is real (see RTL8188EUS_DKMS.md "Weak-AP RX sensitivity").
What this gate SLICES OUT / never demands:
  1. ``_strip_async_watchdog`` removes the phydm watchdog's per-tick ``R REG_SYS_CFG(0xF0)/4``
     sreset read so the synchronous diff lines up. That read is benign, BUT it is the visible
     tip of the runtime DM: the watchdog also runs the DIG burst (FA counters 0xC00/0xD00/
     0xCF0/0xDA*, CCK-PD 0xA5x, AGC 0x8Cx, NHM 0xF8x, EDCCA) which this diff never checks.
  2. Verification STOPS at the monitor opmode entry (M10). The capture continues into the
     airodump operational phase where the vendor DM actually ADAPTS the CCK/AGC registers
     (0xA50/0xA54/0x8C4...). ``verify_channels.py`` replays only the *initial* ch1 tune, so
     that whole operational phase -- where weak-AP RX sensitivity is set -- is unverified.
  3. Not replayed by design: airmon's STA->monitor dance (we are always-monitor); the
     per-hop airodump channel tunes (only ch1 is diffed); the [0..5] chip-version prologue.
LESSON: byte-perfect init can still under-perform on RX because the continuous phydm DM
(CCK-PD + AGC adaptation, beyond the IGI-only ``dig.py``) is the part that wins weak APs,
and it lives in the sliced-out/under-verified region. The live beacon-watch A/B is the only
RX gate; do not read "byte-for-byte PASS" as "RX as good as mainline".
=========================================================================================

Rides the shared rtw88-family replay engine (``scripts/rtw88_pcap_replay``): the 8188e uses
the same Realtek vendor request (bRequest 0x05), and its FW-page control writes map onto the
engine's ``write_block`` alias.

Milestones verified (built up as the port progresses):
  M1  power-on (CARDEMU_TO_ACT + REG_CR), verified from the first op
  M1  firmware download + FW-ready, verified from REG_MCUFWDL (start_addr=0x80)

The efuse probe read (region between power-on and FW) is verified separately once ported;
until then M1 verifies power-on and the FW download as two windows.

    uv run python scripts/rtl8188eus_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8188eus_dkms import (  # noqa: E402
    bb, dig, dm, efuse, firmware, mac, monitor, powertrack, pwrseq, rf, txpower,
)
from wifit3.chips.rtl8188eus_dkms import powertrack_tbl as PT  # noqa: E402

EEPROM_THERMAL_METER_88E = 0xBA
EEPROM_DEFAULT_THERMAL_88E = 0x18
from wifit3.chips.rtl8188eus_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8188eus_dkms.constants import DEFAULT_INIT_CHANNEL  # noqa: E402

REG_MCUFWDL = 0x0080
REG_SYS_CFG = 0x00F0
REG_APS_FSMCO_B2 = 0x0006   # first power-seq op (CARDEMU_TO_ACT step 1 poll)
DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8188eu" / "capture-1.pcap"


def _strip_async_watchdog(ops):
    """Remove the async 2 s-watchdog's per-tick sreset read (R REG_SYS_CFG/4),
    which a background kernel thread interleaves into the single EP0 stream every
    ~2.016 s (first fire ~frame 2731). The synchronous init/tune path never issues
    a 32-bit REG_SYS_CFG read (read_chip_version runs once at probe, before this
    window), so this is unambiguous. The watchdog's *DIG* burst (FA counters, NHM,
    EDCCA) is a separate runtime concern verified by the DIG-watchdog milestone."""
    return [o for o in ops
            if not (o["kind"] == "R" and o.get("addr") == REG_SYS_CFG and o["width"] == 4)]


def _verify_pre_fw(pcap, dev):
    """The probe-phase chain from the first power-seq op (the 0x06 power-ready poll),
    contiguous on one transport (this region is before the watchdog's first fire):
        power-on    (CARDEMU_TO_ACT + REG_CR)
        efuse read  (IOL READ_EFUSE_MAP + packet-buffer readback + PG decode)
    Returns (milestones, ChipParams) -- the decoded crystal_cap feeds the main chain,
    closing the M2b hardcode. (The [0..5] chip-version prologue ahead of 0x06 and the
    MISC01 queue/page setup after the efuse read are not yet ported -- small gaps.)"""
    ops = rp.extract_ops(pcap, dev, start_addr=REG_APS_FSMCO_B2)
    t = rp.ReplayTransport(ops)
    miles = []
    pwrseq.power_on(t)
    miles.append(("power-on", t.i))
    params = efuse.read_chip_params(t)
    miles.append(("efuse", t.i))
    mac.init_misc01(t)
    miles.append(("misc01", t.i))
    return miles, params


def _verify_main_chain(pcap, dev, params):
    """The contiguous post-power-on bring-up, verified from the first REG_MCUFWDL op.
    Each milestone consumes the next span of the same transport byte-for-byte:
        M1   firmware download + FW-ready + InitializeFirmwareVars
        M2a  PHY_MACConfig8188E (MAC reg table + MAX_AGGR_NUM)
        M2b  PHY_BBConfig8188E (BB enable + PHY_REG + AGC_TAB + crystal cap)
        M2c  PHY_RFConfig8188E (RFENV setup + radio_a table + restore)
    The decoded crystal_cap from the probe efuse read feeds M2b (no hardcode)."""
    ops = _strip_async_watchdog(rp.extract_ops(pcap, dev, start_addr=REG_MCUFWDL))
    t = rp.ReplayTransport(ops)
    miles = []
    firmware.download_firmware(t, firmware.load_firmware_blob())
    miles.append(("M1 fw", t.i))
    mac.phy_mac_config(t)
    miles.append(("M2a mac", t.i))
    bb.phy_bb_config(t, crystal_cap=params.crystal_cap)
    miles.append(("M2b bb", t.i))
    rf.phy_rf_config(t)
    miles.append(("M2c rf", t.i))
    efuse.iol_efuse_patch(t)
    miles.append(("M2d efpatch", t.i))
    mac.init_tx_buffer_boundary(t)
    mac.init_llt(t)
    miles.append(("M2e llt", t.i))
    mac.init_misc02(t)
    miles.append(("M3 misc02", t.i))
    rf.read_rf_chnl_val(t)
    miles.append(("M4a rfchnl", t.i))
    bb.bb_turn_on_block(t)
    miles.append(("M4b bbturn", t.i))
    mac.invalidate_cam_all(t)
    miles.append(("M4c cam", t.i))
    txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL)
    miles.append(("M5 txpwr", t.i))
    mac.init_misc11_tail(t)
    miles.append(("M6 misc11", t.i))
    dm.init_hal_dm(t)
    miles.append(("M7 inithaldm", t.i))
    dm.init_hal_tail(t)
    miles.append(("M8 haltail", t.i))
    return miles


def verify_monitor_block(ops) -> tuple:
    """Targeted diff of the monitor opmode entry (M10).

    wifit3 enters monitor directly, so it does NOT replay airmon's STA->monitor dance the
    cold-boot pcap shows. The monitor entry is verified as a standalone block, anchored on the
    monitor RCR write (W REG_RCR=RCR_MONITOR_VALUE): the 5 vendor ops (MSR read/write, RCR
    backup-read/write, RXFLTMAP2 write) byte-diff against the wire; the 2 RXFLTMAP0/1 opens are
    wifit3's monitor-breadth additions (not on the wire) so they are appended as expected ops.
    """
    k = next((i for i, o in enumerate(ops)
              if o["kind"] == "W" and o.get("addr") == C.REG_RCR
              and o.get("value") == C.RCR_MONITOR_VALUE), None)
    if k is None:
        raise rp.Divergence(f"monitor RCR write (0x608={C.RCR_MONITOR_VALUE:#x}) not in capture")
    wire = ops[k - 3:k + 2]                  # MSR r/w, RCR r/w, RXFLTMAP2 w (5 vendor ops)
    additions = [{"kind": "W", "addr": C.REG_RXFLTMAP0, "value": 0xFFFF, "width": 2},
                 {"kind": "W", "addr": C.REG_RXFLTMAP1, "value": 0xFFFF, "width": 2}]
    t = rp.ReplayTransport(wire + additions)
    monitor.enter_monitor(t)
    if t.i != len(wire) + len(additions):
        raise rp.Divergence(f"monitor block: port emitted {t.i} of {len(wire) + 2} ops")
    return len(wire), len(additions)


def _seed_powertrack(dm_ops, params):
    """Seed the thermal-tracking state from the capture: the default swing indices from
    the InitHalDm 0xc80/0xa22 reads, the thermal base from the efuse (the driver re-reads
    these live at watchdog start; the replay covers only the tick, so they're seeded)."""
    c80 = next((o["value"] for o in dm_ops
                if o["kind"] == "R" and o.get("addr") == 0x0C80 and o["width"] == 4), 0)
    a22 = next((o["value"] for o in dm_ops
                if o["kind"] == "R" and o.get("addr") == 0x0A22 and o["width"] == 1), 0)
    raw = params.efuse_map[EEPROM_THERMAL_METER_88E]
    eeprom_thermal = EEPROM_DEFAULT_THERMAL_88E if raw == 0xFF else raw
    ofdm_i = powertrack.get_swing_index(c80)
    cck_i = powertrack.get_cck_swing_index(a22)
    return powertrack.PowerTrackState(
        eeprom_thermal=eeprom_thermal,
        default_ofdm_index=ofdm_i if ofdm_i < len(PT.OFDM_SWING_TABLE) else 30,
        default_cck_index=cck_i if cck_i < len(PT.CCK_SWING_TABLE_CH1_CH13) else 20,
    )


def verify_dm_tick(dm_ops, params) -> tuple:
    """Targeted byte-diff of one no-link phydm DM watchdog tick — the operational-phase
    async stream the synchronous init diff strips (the paired ``verify_`` for the stripped
    watchdog, per PORTING.md "strip, but never forget").

    Anchored on the FA-counter hold (the first 0xC00 touch after the monitor entry), it
    seeds the carried DM state from the chip's post-InitHalDm values (IGI 0x20; the CCK CCA
    default ``phydm_cck_pd_init`` reads from 0xa08[23:16], recovered from the capture's own
    InitHalDm read) and replays ``dig.watchdog_tick`` against the wire. Because the DM
    carries state, this verifies the **first** operational tick, where those seeds still
    hold. Returns ``(start_op, consumed, next_unported)`` — ``next_unported`` is the first
    tick op the port does not yet emit (the TDD pointer to the next mechanism), None once
    the whole tick is ported."""
    mon = next((i for i, o in enumerate(dm_ops)
                if o["kind"] == "W" and o.get("addr") == C.REG_RCR
                and o.get("value") == C.RCR_MONITOR_VALUE), None)
    if mon is None:
        raise rp.Divergence("monitor entry not found — no DM-tick anchor")
    start = next((i for i in range(mon, len(dm_ops))
                  if dm_ops[i].get("addr") == 0x0C00), None)
    if start is None:
        raise rp.Divergence("no FA-counter hold (0xC00) after monitor entry")
    a0a = next(((o["value"] >> 16) & 0xFF for o in dm_ops
                if o["kind"] == "R" and o.get("addr") == 0x0A08), 0)
    state = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=a0a)
    pt_state = _seed_powertrack(dm_ops, params)
    t = rp.ReplayTransport(dm_ops[start:])
    dig.watchdog_tick(t, state, pt_state)
    nxt = dm_ops[start + t.i] if start + t.i < len(dm_ops) else None
    return start, t.i, nxt


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    ops = rp.extract_ops(pcap, dev)
    print(f"{pcap.name}: card=dev{dev}, {len(ops)} vendor ops")

    try:
        pre_miles, params = _verify_pre_fw(pcap, dev)
    except rp.Divergence as e:
        print(f"\nFAIL pre-FW @ first divergence:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug): {type(e).__name__}: {e}")
        return 2

    prev = 0
    for label, end in pre_miles:
        print(f"  PASS {label:10} {end - prev:5} ops")
        prev = end
    mac = params.mac_address
    mac_show = (f"{mac[0]:02x}:{mac[1]:02x}:{mac[2]:02x}:xx:xx:xx (OUI)"
                if len(mac) == 6 else "invalid")
    print(f"       efuse: crystal_cap=0x{params.crystal_cap:02x}, mac={mac_show}")

    try:
        miles = _verify_main_chain(pcap, dev, params)
    except rp.Divergence as e:
        print(f"\nFAIL @ first divergence:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug): {type(e).__name__}: {e}")
        return 2

    prev = 0
    for label, end in miles:
        print(f"  PASS {label:10} {end - prev:5} ops")
        prev = end

    # M10 monitor entry — a targeted out-of-line block (airmon-divergent; see the function).
    try:
        ops = _strip_async_watchdog(rp.extract_ops(pcap, dev, start_addr=REG_MCUFWDL))
        nvendor, nadd = verify_monitor_block(ops)
    except rp.Divergence as e:
        print(f"\nFAIL monitor block:\n  {e}")
        return 1
    print(f"  PASS M10 monitor   {nvendor} vendor ops + {nadd} wifit3 RXFLTMAP opens")

    # Operational-phase async stream: one no-link phydm DM watchdog tick (the paired
    # verify_ for the stripped watchdog). Coverage of the tick grows as mechanisms land.
    try:
        dm_ops = rp.extract_ops(pcap, dev)               # full stream (the tick IS async)
        start, consumed, nxt = verify_dm_tick(dm_ops, params)
    except rp.Divergence as e:
        print(f"\nFAIL DM tick:\n  {e}")
        return 1
    if nxt is None:
        print(f"  PASS DM tick @op{start}: {consumed} ops byte-faithful (whole tick)")
    else:
        nxt_s = (f"{nxt['kind']} 0x{nxt['addr']:04x}/{nxt['width']}=0x{nxt['value']:x}"
                 if nxt["kind"] != "B" else "bulk")
        print(f"  ~~~~ DM tick @op{start}: {consumed} ops byte-faithful; "
              f"next un-ported op = {nxt_s}")

    print("\nPASS: power-on + efuse + firmware + MAC/BB/RF/efuse-patch/LLT + "
          "InitHalDm + tail + monitor byte-for-byte.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
