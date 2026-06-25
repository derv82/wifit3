"""Acceptance gate: replay-diff the clean-room ar9271_v2 port against its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed — the model from ``scripts/rt5370/verify_pcap.py``
and ``scripts/rtl8821cu_dkms/verify_pcap.py`` (async producers dispatched to real handlers at
the cursor). The replay is at the USB layer (``ar9271_pcap_replay.ReplayDevice``) and the REAL
``chips/ar9271_v2`` transport drives it, so each milestone's handler replays with zero
reimplementation.

  * **matched**     — the port's real handler reproduces the op byte-for-byte at the cursor.
  * **waived**      — an explicit, *named*, *counted* boundary for a producer that is NOT the
                      ath9k_htc kernel driver (e.g. aireplay-ng's TX from the human-fired
                      injection at the capture tail).
  * **unaccounted** — anything else STOPS the walk and names the op. That op IS the porting
                      frontier: the next op to reproduce.

This port is built in milestones; until the last lands, a clean run ends at the FRONTIER (the
first not-yet-ported op), not PASS. That is the expected, honest state mid-port — the frontier
op names exactly where the next milestone begins.

RULES (do not violate — this is the whole point of the gate):
  * NEVER edit this file to make it print PASS.
  * NEVER read chips/ar9271/ (the v1 port) — v2 is a blind re-port from the kernel C in
    data_dumps/ath9k-source-v6.18/. Let THIS wire confirm it.
  * The cursor only advances by reproducing the wire or by an explicit named waiver.

    uv run python scripts/verify_pcap.py ar9271_v2 [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "ar9271_v2"))

import ar9271_pcap_replay as rp  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_ath9k_htc_newddevice"

_IMPORT_ERR = None
try:
    from wifit3.chips.ar9271_v2 import ani, chan as chanmod, constants as C, eeprom, firmware, gpio, htc, hw, key, phy, reg as R  # noqa: E402
    from wifit3.chips.ar9271_v2.wmi import WMI               # noqa: E402
    from wifit3.chips.ar9271_v2.transport import AR9271Transport  # noqa: E402
except ImportError as e:                                  # driver not scaffolded yet
    _IMPORT_ERR = e


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler against the wire
    from the cursor (a fresh ReplayDevice over the remaining ops, wrapped in the real chip
    transport); ``waive`` consumes one op of a named non-reproduced producer. Both advance."""

    def __init__(self, ops: list[dict], responses: list[dict]):
        self.ops = ops
        self.responses = responses
        self.i = 0
        self.resp_pos: dict[int, int] = {}
        self.wmi = None                     # persistent WMI channel, bound after the handshake
        self.hw = None                      # persistent AthHw, created at chip reset
        self.chan = None                    # the channel being brought up
        self.value_except_regs: set[int] = set()   # registers whose written value is excepted
        self.value_excepted = 0
        self.waived: Counter = Counter()

    def run(self, fn, label: str):
        """Drive a port handler from the cursor. Both the host-op cursor and the per-ep
        response position persist across calls, so a multi-step WMI conversation (one seq
        counter, one continuous REG_IN stream) replays unbroken."""
        rd = rp.ReplayDevice(self.ops, self.responses, op_start=self.i, resp_pos=self.resp_pos,
                             value_except_regs=self.value_except_regs)
        t = AR9271Transport(rd)
        if self.wmi is not None:
            self.wmi.t = t                  # rebind the persistent WMI to this call's transport
        result = fn(t)
        self.i = rd.i
        self.resp_pos = rd.resp_pos
        self.value_excepted += rd.value_excepted
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _walk_init(w: Walk) -> None:
    """Cold bring-up, one cursor, no re-anchoring. WIRE ORDER (ath9k_htc):
    firmware download -> [M2 frontier: HTC/WMI handshake + ath9k_hw init].

    Source: data_dumps/ath9k-source-v6.18/ath9k/hif_usb.c.

    firmware   13x 4096B RAM writes (bRequest 0x30) + COMP (0x31)   ath9k_hif_usb_download_fw
    htc        READY -> 9x connect_service -> config credits ->      htc_hst.c / htc_drv_init.c
               setup complete
    reset      SREV read + power-on chip reset (RTC/RC block) +       hw.c __ath9k_hw_init
               setpower(AWAKE) + AR_PHY_CHIP_ID read
    rf_claim   AR_PHY(0) seed + radio-rev probe                       ar9002_hw.c post_init
    eeprom     4k map fill (188 words) + magic/checksum/version       eeprom_4k.c / eeprom.c
    --- M2b frontier: ar9002 analog/initvals tables / ani / calibration ---
    """
    fw = firmware.load_firmware_blob()
    w.run(lambda t: firmware.download(t, fw), "firmware")
    st = w.run(lambda t: htc.handshake(t), "htc-handshake")

    w.wmi = WMI(None, ctrl_epid=st.endpoints[C.WMI_CONTROL_SVC])
    w.hw = w.run(lambda t: hw.init_reset(w.wmi), "chip-reset")
    w.run(lambda t: phy.rf_claim(w.hw), "rf-claim")
    w.run(lambda t: eeprom.init(w.hw), "eeprom")
    w.run(lambda t: ani.ani_init(w.hw), "ani-init")
    w.run(lambda t: key.init_crypto(w.hw), "key-cache-clear")
    w.run(lambda t: w.wmi.get_fw_version(), "get-fw-version")
    # ath9k_htc_start's wake path: a COLD chip reset (chip was FULL_SLEEPed after probe),
    # then init_pll for the initial channel (mac80211 default = ch1, 2412 MHz).
    w.chan = chanmod.channel_2ghz(1)
    w.run(lambda t: w.hw.set_reset_reg(R.ATH9K_RESET_COLD), "chip-reset-cold")
    w.run(lambda t: w.hw.init_pll(w.chan), "init-pll")
    w.run(lambda t: gpio.led_init(w.hw), "led-gpio")
    from wifit3.chips.ar9271_v2.wmi import WMI_FLUSH_RECV_CMDID
    w.run(lambda t: w.wmi.cmd(WMI_FLUSH_RECV_CMDID, b""), "flush-recv")
    # ath9k_hw_reset opening: preamble saves + chip_reset(WARM) + init_pll + MAC-gate +
    # TSF restore (low word value-excepted: tsf+wall-clock-offset) + JTAG disable.
    w.value_except_regs.add(R.AR_TSF_L32)
    w.run(lambda t: w.hw.reset_begin(w.chan), "hw-reset-begin")
    w.run(lambda t: phy.process_ini(w.hw, w.chan), "process-ini")
    # ath9k_hw_reset tail: set_rfmode -> init_mfp -> set_delta_slope -> spur_mitigate.
    w.run(lambda t: phy.set_rfmode(w.hw, w.chan), "set-rfmode")
    w.run(lambda t: w.hw.init_mfp(), "init-mfp")
    w.run(lambda t: phy.set_delta_slope(w.hw, w.chan), "set-delta-slope")
    w.run(lambda t: phy.spur_mitigate(w.hw, w.chan), "spur-mitigate")
    from wifit3.chips.ar9271_v2 import phy_board
    w.run(lambda t: phy_board.set_board_values(w.hw, w.chan), "set-board-values")
    w.run(lambda t: w.hw.reset_opmode(w.hw.macStaId1, w.hw.saveDefAntenna), "reset-opmode")
    w.run(lambda t: phy.rf_set_freq(w.hw, w.chan), "rf-set-freq")
    from wifit3.chips.ar9271_v2 import mac_queue
    mac_queue.init_tx_queues(w.hw)                       # driver-side alloc (no wire ops)
    w.run(lambda t: mac_queue.init_queues(w.hw), "init-queues")
    w.run(lambda t: w.hw.init_interrupt_masks(), "init-interrupt-masks")
    w.run(lambda t: w.hw.ani_cache_ini_regs(), "ani-cache-ini-regs")
    w.run(lambda t: w.hw.init_qos(), "init-qos")
    w.run(lambda t: w.hw.init_global_settings(w.chan), "init-global-settings")
    w.run(lambda t: w.hw.reset_dma_and_intr(), "set-dma-obs-rimt")
    w.run(lambda t: phy.init_bb(w.hw, w.chan), "init-bb")
    from wifit3.chips.ar9271_v2 import calib
    w.run(lambda t: calib.init_cal(w.hw, w.chan), "init-cal")
    w.run(lambda t: w.hw.reset_tail(), "reset-tail")
    # ath9k_htc_start tail [SRC] htc_drv_main.c:941: re-apply tx power (priv->txpowlimit=0 at
    # first start), then SET_MODE(11ng) / ATH_INIT / START_RECV.
    import struct
    from wifit3.chips.ar9271_v2 import phy_power
    from wifit3.chips.ar9271_v2.wmi import (
        WMI_ATH_INIT_CMDID, WMI_SET_MODE_CMDID, WMI_START_RECV_CMDID)
    w.run(lambda t: phy_power.update_txpow(w.hw, w.chan, 0), "update-txpow")
    w.run(lambda t: w.wmi.cmd(WMI_SET_MODE_CMDID, struct.pack(">H", 1)), "wmi-set-mode")
    w.run(lambda t: w.wmi.cmd(WMI_ATH_INIT_CMDID, b""), "wmi-ath-init")
    w.run(lambda t: w.wmi.cmd(WMI_START_RECV_CMDID, b""), "wmi-start-recv")
    from wifit3.chips.ar9271_v2 import rx
    w.run(lambda t: rx.host_rx_init(w.hw), "host-rx-init")
    w.run(lambda t: w.wmi.update_cap_target(w.hw.txchainmask), "update-cap-target")


def run(cap: str | None = None) -> int:
    if _IMPORT_ERR is not None:
        print(f"ar9271_v2 driver not importable yet: {_IMPORT_ERR}")
        return 2

    name = cap or "capture-1"
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"capture not found: {pcap}")
        return 2

    pkts = rp.parse_pcapng(str(pcap))
    dev = rp.detect_card(pkts)
    if dev is None:
        print(f"no AR9271 firmware-download traffic found in {pcap.name}")
        return 2
    ex = rp.extract(pkts, dev)
    ops, responses = ex["host_ops"], ex["responses"]
    w = Walk(ops, responses)

    print(f"ar9271_v2 verify — {pcap.name}: devnum {dev}, {len(ops)} host ops, "
          f"{len(responses)} device responses")

    try:
        _walk_init(w)
    except rp.Divergence as d:
        print(f"\nDIVERGENCE at op #{w.i}: {d}")
        return 1

    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    if w.value_excepted:
        print(f"  value-excepted {w.value_excepted} write(s) — hardware/wall-clock value "
              "(register + headers matched)")

    if w.i < len(ops):
        front = w.peek()
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.fmt_op(front)}")
        print("  ^ the next op to reproduce (port the next milestone, or add a named waiver).")
        # Mid-port milestone markers: each frontier op names where the next milestone begins.
        if w.i == 14:
            print("  M1 OK: 14 firmware-download control ops matched; frontier is the HTC "
                  "handshake (M2a).")
        elif w.i == 25:
            print("  M2a OK: firmware + HTC handshake matched; frontier is the WMI register "
                  "init (M2b).")
        elif w.i == 42:
            print("  M2b-1 OK: + SREV read & power-on chip reset matched; frontier is "
                  "ath9k_hw_setpower(AWAKE).")
        elif w.i == 47:
            print("  M2b-2 OK: + setpower(AWAKE) & phyRev read matched; frontier is the PHY "
                  "init writes (analog/initvals).")
        elif w.i == 77:
            print("  M2b-3 OK: + rf_claim & 4k EEPROM fill/validate matched; frontier is the "
                  "ar9002 analog/initvals tables.")
        elif w.i == 81:
            print("  M2c-1 OK: + ANI init (PHY-error + MIB counters) matched.")
        elif w.i == 337:
            print("  M2c-2 OK: + key-cache clear (128 entries) matched.")
        elif w.i == 344:
            print("  M2c-3 OK: + WMI_GET_FW_VERSION & htc-start COLD chip reset matched; "
                  "frontier is init_pll (channel-aware).")
        elif w.i == 347:
            print("  M2c-4 OK: + channel model & init_pll matched; frontier is GPIO config.")
        elif w.i == 351:
            print("  M2c-5 OK: + LED GPIO config & WMI_FLUSH_RECV matched; frontier is "
                  "ath9k_hw_reset (DEF_ANTENNA read).")
        elif w.i == 371:
            print("  M2d-1 OK: + ath9k_hw_reset preamble & chip_reset(WARM) matched; frontier "
                  "is the TSF restore (wall-clock-dependent value).")
        elif w.i == 374:
            print("  M2d-2 OK: + TSF restore (value-excepted) & JTAG disable matched; frontier "
                  "is ath9k_hw_process_ini (the initvals tables).")
        elif w.i == 389:
            print("  M2d-3 OK: + process_ini initvals (iniModes + txgain + iniCommon) matched; "
                  "frontier is override_ini / set_channel_regs.")
        elif w.i == 394:
            print("  M2d-4 OK: + override_ini & set_channel_regs (20 MHz) matched; frontier is "
                  "init_chain_masks / RF banks.")
        elif w.i == 395:
            print("  M2d-5 OK: + init_chain_masks (1T1R) matched; frontier is the "
                  "txpower / RF-bank writes.")
        elif w.i == 398:
            print("  M2d-6 OK: + apply_txpower (TPCRG1 gain cfg + 32 PDADC words + per-rate "
                  "power) matched; frontier is set_rfmode / rf_set_freq.")
        elif w.i == 405:
            print("  M2e-1 OK: + set_rfmode, init_mfp, set_delta_slope, spur_mitigate matched; "
                  "frontier is eep set_board_values (antenna/gain modal config).")
        elif w.i == 415:
            print("  M2e-2 OK: + eep set_board_values (switch/gain/analog-bias/settling) "
                  "matched; frontier is reset_opmode (STA_ID1 / bssidmask / antenna).")
        elif w.i == 419:
            print("  M2e-3 OK: + reset_opmode (STA id/defaults, bssidmask, antenna, associd, "
                  "STATION mode) matched; frontier is rf_set_freq (synthesizer).")
        elif w.i == 423:
            print("  M2e-4 OK: + rf_set_freq (2.4 GHz CHANSEL synthesizer) matched; frontier "
                  "is init_queues (QCU/DCU setup).")
        elif w.i == 441:
            print("  M2e-5 OK: + init_queues (DQCUMASK + 4 data/CAB/beacon DCU config + per-"
                  "queue IMR) matched; frontier is init_interrupt_masks.")
        elif w.i == 450:
            print("  M2e-6 OK: + init_interrupt_masks, ani_cache_ini_regs, init_qos matched; "
                  "frontier is init_global_settings / PCU.")
        elif w.i == 459:
            print("  M2e-7 OK: + init_global_settings (SIFS/slot/ACK/CTS/EIFS/USEC timing) "
                  "matched; frontier is STA_ID1 PRESERVE_SEQNUM / set_dma.")
        elif w.i == 467:
            print("  M2e-8 OK: + STA_ID1 seqnum, set_dma, AR_OBS, RX-intr-mitigation matched; "
                  "frontier is init_bb / calibration.")
        elif w.i == 469:
            print("  M2e-9 OK: + init_bb (AR_PHY_ACTIVE enable) matched; frontier is "
                  "init_cal (IQ/ADC calibration).")
        elif w.i == 520:
            print("  M3 OK: + init_cal (cl_cal carrier-leak, ar9271 pa_cal, loadnf, "
                  "start_nfcal, IQ-cal setup) matched; frontier is the reset tail "
                  "(AR_CFG_LED / restore_chainmask / gen_timer / init_desc).")
        elif w.i == 522:
            print("  M4 OK: + ath9k_hw_reset tail (LED+32kHz, init_desc AR9271 byte-swap; "
                  "restore_chainmask/gen_timer/gpio-override are no-ops) matched; frontier is "
                  "the post-reset htc-start (txpower update + WMI mode/init/start-recv).")
        elif w.i == 528:
            print("  M5 OK: + htc-start tail (txpowlimit=0 update clamps rates to 0x0a, then "
                  "WMI SET_MODE(11ng)/ATH_INIT/START_RECV) matched; frontier is the RX path "
                  "(host_rx_init / cap-target update).")
        elif w.i == 541:
            print("  M6 OK: + host_rx_init (rxena, STA rx/mcast filters, startpcureceive: "
                  "mib + ani_reset + DIAG-RX clear) and WMI_TARGET_IC_UPDATE matched; frontier "
                  "is the channel-config / tune sweep.")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
