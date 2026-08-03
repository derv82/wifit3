"""Acceptance gate: replay-diff the clean-room ar9271_v2 port against its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed — modelled on ``scripts/chips/rtl8821cu_dkms/verify_pcap.py``
(async producers dispatched to real handlers at the cursor). The replay is at the USB layer
(``ar9271_pcap_replay.ReplayDevice``) and the REAL ``chips/ar9271_v2`` driver drives it: the gate
builds ``AR9271V2Driver`` over the ReplayDevice transport and calls its PUBLIC interface —
``connect`` (firmware + cold bring-up + monitor entry), ``set_channel`` (the channel-hop sweep) and
``inject_frame`` (the aireplay-ng TX) — so the bytes verified are exactly the product's, with zero
reimplementation in the gate.

  * **matched**       — the driver's real method reproduces the op byte-for-byte at the cursor.
  * **value-excepted**— a write whose register + headers match but whose VALUE is host/hardware
                        state with no wire/eeprom signature (TSF restore; the ch14 regulatory
                        per-rate power). Named, counted, never silently waived.
  * **cookie-excepted**— a TX frame matching the wire except its single TX-slot cookie byte (the
                        host-scheduling allocation race). Named, counted.
  * **unaccounted**   — anything else STOPS the walk and names the op: a real divergence to fix.

The cold init + hop sequencing now lives in the driver (``bringup.py`` / ``driver.set_channel``);
this gate only drives the public methods and reproduces the driver-autonomous async producers
interleaved into the stream (the LED-blink timer's GPIO writes, the sw-scan configure_filter).

RULES (do not violate — this is the whole point of the gate):
  * NEVER edit this file to make it print PASS.
  * NEVER read chips/ar9271/ (the v1 port) — v2 is a blind re-port from the kernel C in
    driver_sources/ath9k-source-v6.18.12/. Let THIS wire confirm it.
  * The cursor only advances by reproducing the wire or by an explicit named exception.

    uv run python scripts/porting/verify_pcap.py ar9271_v2 [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))
sys.path.insert(0, str(REPO / "scripts" / "chips" / "ar9271_v2"))

import ar9271_pcap_replay as rp

CAP_DIR = REPO / "driver_captures" / "captures_ath9k_htc_newddevice"

_IMPORT_ERR = None
try:
    from wifit3.chips.ar9271_v2 import constants as C, gpio, reg as R, rx, tx
    from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
except ImportError as e:                                  # driver not scaffolded yet
    _IMPORT_ERR = e

# ch14 (2484) reduces the OFDM/HT per-rate power 1 dB vs every other 2.4 GHz channel — a cfg80211
# NO_OFDM regulatory rule for ch14, host state with no USB-wire or eeprom signature (the kernel's
# set_4k_txpower gives ch1 and ch14 identical output; see AR9271_V2.md). The per-rate registers
# are value-excepted for the ch14 hop ONLY; every other channel must match exactly.
_CH14_REGS = {R.AR_PHY_POWER_TX_RATE1, R.AR_PHY_POWER_TX_RATE2, R.AR_PHY_POWER_TX_RATE3,
              R.AR_PHY_POWER_TX_RATE4, R.AR_PHY_POWER_TX_RATE5, R.AR_PHY_POWER_TX_RATE6}


def _run_coro(coro):
    """Drive a driver coroutine to completion. Replay transport I/O is synchronous (no running
    loop -> the driver's inline path), so one ``send`` runs it straight to its ``return``."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise RuntimeError("driver coroutine suspended on a real await; replay is synchronous")


class Walk:
    """One ReplayDevice cursor over the whole capture, driving the real driver. The device cursor
    advances by the ops each public-method call consumed; session state (wmi seq, hw shadows) lives
    on the driver. The TSF restore is value-excepted from the start (the low word is always
    wall-clock-dependent)."""

    def __init__(self, ops: list[dict], responses: list[dict]):
        self.ops = ops
        self.dev = rp.ReplayDevice(ops, responses, value_except_regs={R.AR_TSF_L32})
        self.driver = AR9271V2Driver(self.dev)        # the driver wraps dev in AR9271Transport
        self.chan = None                              # the channel being brought up / hopped to
        self.async_injected = 0                       # LED-blink ops reproduced at the cursor
        # TX-status completions (REG_IN WMI_TXSTATUS events) free the slots the injects allocate;
        # collect them up front and replay them interleaved by frame (see drain_tx_status).
        self.txs_events = [
            (r["frame"], bytes(r["data"])[C.HTC_FRAME_HDR_LEN + 4:])
            for r in responses
            if r["ep"] == 0x83 and len(bytes(r["data"])) >= C.HTC_FRAME_HDR_LEN + 4
            and struct.unpack_from(">H", bytes(r["data"]), C.HTC_FRAME_HDR_LEN)[0]
            == tx.WMI_TXSTATUS_EVENTID
        ]
        self.txs_i = 0

    @property
    def i(self) -> int:
        return self.dev.i

    def peek(self) -> dict | None:
        return self.ops[self.dev.i] if self.dev.i < len(self.ops) else None

    # ---- the walk ---------------------------------------------------------
    def run(self) -> None:
        """Cold bring-up through the channel-hop + inject sweep, one cursor, no re-anchoring."""
        _run_coro(self.driver.connect())
        # The LED-blink timer interleaves GPIO RMWs into the WMI command stream during the
        # operational sweep; reproduce them in place via the real gpio.set_gpio so the bytes and
        # the shared WMI seq counter stay aligned (a waiver would desync every later op).
        self.driver.wmi.async_injector = self._drain_async
        self.dev.tx_mgmt_epid = self.driver.mgmt_epid   # arms the TX cookie-byte exception
        self._operational_sweep()

    def _operational_sweep(self) -> None:
        """Each ath9k_htc_set_channel re-tunes via driver.set_channel (full or fast, read off the
        wire); some hops are bracketed by sw-scan configure_filter calls and LED blinks; the
        aireplay-ng phases bulk-OUT TX frames the driver rebuilds via inject_frame. Drive all off
        the wire until an op opens no handler (the frontier)."""
        while True:
            op = self.peek()
            if op is not None:
                self.drain_tx_status(op.get("frame", 0))     # free slots completed before this op
            if op is not None and op.get("ep") == 0x01:      # bulk-OUT TX -> aireplay-ng injection
                dot11 = tx.dot11_from_bulk(bytes(op.get("data") or b""))
                self.driver._emit_frame(dot11)               # sync core (public inject_frame is async)
                continue
            cmd, reg, set_bits = self._peek_wmi()
            if cmd == 0x04:                                  # WMI_DISABLE_INTR -> a channel hop
                self.chan = self._next_synth_channel()
                fastcc = not self._hop_is_full_reset()
                self._ch14_except(True)
                _run_coro(self.driver.set_channel(self.chan.channel, _fastcc=fastcc))
                self._ch14_except(False)
            elif cmd == 0x14 and reg == R.AR_RX_FILTER:      # a configure_filter (getrxfilter first)
                rx.configure_filter(self.driver.hw, self.driver.hw.rxfilter_flags)
            elif cmd == 0x20 and reg == R.AR_GPIO_IN_OUT:    # LED blink (set_gpio on pin 15)
                val = 1 if set_bits == 0 else 0
                gpio.set_gpio(self.driver.hw, R.ATH_LED_PIN_9271, val)
            else:
                break

    # ---- async-producer + wire-peek helpers -------------------------------
    def drain_tx_status(self, frame: int) -> None:
        """Feed the driver every WMI_TXSTATUS completion the kernel processed before ``frame`` —
        freeing TX slots so the next inject's find_first_zero_bit cookie matches the wire."""
        while self.txs_i < len(self.txs_events) and self.txs_events[self.txs_i][0] < frame:
            self.driver.tx_status_event(self.txs_events[self.txs_i][1])
            self.txs_i += 1

    def _ch14_except(self, on: bool) -> None:
        if self.chan is None or self.chan.channel != 14:
            return
        if on:
            self.dev.value_except_regs |= _CH14_REGS
        else:
            self.dev.value_except_regs -= _CH14_REGS

    def _drain_async(self, command_id: int, payload: bytes) -> None:
        """Reproduce the ath9k_htc LED-blink timer's GPIO RMW the kernel interleaved at the cursor,
        through its real handler (gpio.set_gpio), so both the bytes and the shared WMI seq counter
        stay aligned. Registered as wmi.async_injector, run at the head of every cmd. A cmd that is
        itself a GPIO_IN_OUT RMW (a handler's own set_gpio, or the one injected here) passes
        through untouched — which also makes this non-recursive."""
        hw = self.driver.hw
        if (command_id == 0x20 and len(payload) >= 4
                and struct.unpack_from(">I", payload, 0)[0] == R.AR_GPIO_IN_OUT):
            return
        while True:
            op = self.dev.peek()
            if op is None or op.get("ep") != 0x04:
                return
            b = bytes(op.get("data") or b"")
            if len(b) < 24 or struct.unpack_from(">H", b, 8)[0] != 0x20:
                return
            reg, set_bits = struct.unpack_from(">II", b, 12)
            if reg != R.AR_GPIO_IN_OUT:
                return
            val = 1 if set_bits == 0 else 0       # set_gpio re-inverts on AR9271 [SRC] hw.c:2835
            gpio.set_gpio(hw, R.ATH_LED_PIN_9271, val)
            self.async_injected += 1

    def _peek_wmi(self):
        """Decode the op at the cursor enough to route the sweep: returns (command_id, first
        register, first set-bits). register/set-bits are filled for REG_READ (0x14) / REG_RMW
        (0x20)."""
        op = self.peek()
        if not op or op.get("ep") != 0x04:
            return None, None, None
        b = bytes(op.get("data") or b"")
        if len(b) < 12:
            return None, None, None
        cmd = struct.unpack_from(">H", b, 8)[0]
        reg = set_bits = None
        if cmd == 0x14 and len(b) >= 16:
            reg = struct.unpack_from(">I", b, 12)[0]
        elif cmd == 0x20 and len(b) >= 20:
            reg, set_bits = struct.unpack_from(">II", b, 12)
        return cmd, reg, set_bits

    def _hop_is_full_reset(self) -> bool:
        """Look past the stop sequence + getnf of the upcoming hop: a full ath9k_hw_reset reads
        AR_DEF_ANTENNA (reset_begin), a fast channel change reads AR_CFG first (do_fastcc's
        check_alive). Whichever appears first within the window decides the path."""
        for j in range(self.i, min(self.i + 20, len(self.ops))):
            op = self.ops[j]
            b = bytes(op.get("data") or b"")
            if op.get("ep") != 0x04 or len(b) < 16 or struct.unpack_from(">H", b, 8)[0] != 0x14:
                continue
            reg = struct.unpack_from(">I", b, 12)[0]
            if reg == R.AR_DEF_ANTENNA:
                return True
            if reg == R.AR_CFG:
                return False
        return False

    def _next_synth_channel(self):
        """Peek ahead to the next AR_PHY_SYNTH_CONTROL (0x9874) write and decode the channel it
        tunes — the mac80211 scan order is the capture's input, so we read the requested frequency
        off the wire and let the driver reproduce the per-channel registers."""
        from wifit3.chips.ar9271_v2 import chan as chanmod
        for j in range(self.i, len(self.ops)):
            op = self.ops[j]
            data = op.get("data")
            if not data or op.get("ep") != 0x04:
                continue
            b = bytes(data)
            if len(b) < 12 or struct.unpack_from(">H", b, 8)[0] != 0x15:
                continue
            payload = b[12:]
            for k in range(0, len(payload) - 4, 8):
                reg, val = struct.unpack_from(">II", payload, k)
                if reg == 0x9874:
                    freq = round((val & 0x00FFFFFF) * 15 / 0x10000)
                    ch = (freq - 2407) // 5 if freq != 2484 else 14
                    return chanmod.Channel(channel=ch, center_freq=freq)
        return None


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
        w.run()
    except rp.Divergence as d:
        print(f"\nDIVERGENCE at op #{w.i}: {d}")
        return 1

    if w.dev.value_excepted:
        print(f"  value-excepted {w.dev.value_excepted} write(s) — TSF restore (wall-clock value; "
              "register + headers matched)")
    if w.async_injected:
        print(f"  reproduced {w.async_injected} interleaved LED-blink op(s) via gpio.set_gpio "
              "(byte-matched, seq-aligned)")
    if w.dev.multi_value_excepted:
        print(f"  ch14-power-excepted {w.dev.multi_value_excepted} per-rate write(s) — cfg80211 "
              "ch14 OFDM/HT regulatory limit (host state, not wire/eeprom-derivable)")
    if w.dev.cookie_excepted:
        print(f"  cookie-excepted {w.dev.cookie_excepted} TX frame(s) — TX-slot allocation race "
              "(host scheduling; full HIF/htc/tx headers + 802.11 matched, only the cookie byte "
              "differs)")

    if w.i < len(ops):
        front = w.peek()
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.fmt_op(front)}")
        print("  ^ the next op to reproduce, or a real divergence in the converged driver path.")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly excepted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
