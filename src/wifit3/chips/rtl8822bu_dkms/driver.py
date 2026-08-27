"""RTL8822BU / 2T2R driver — vendor (HALMAC/PHYDM) cleanroom port.

`connect()` runs the deterministic cold bring-up the byte-for-byte gate verifies
(`scripts/chips/rtl8822bu_dkms/verify_pcap.py` → `bringup.cold_bringup`): the entire vendor `rtl8822b_init`
— chip-ID/USB-PHY → EFUSE → two-cycle power/FW/MAC → BB/AGC/crystal/RF tables → full `odm_dm_init`
(RX seed + RF-cal tail) → `phy_bf_init`/wifi-only-coex/`init_misc`. It then tunes to the default
channel (`chan.set_channel_bw`, byte-verified against the capture's airodump hops), starts the bulk-IN
RX reader, and runs the airmon monitor RX-enable (`mac.enable_monitor`, gate-verified against
the capture's monitor switch: MSR no-link, RCR=AAP|APP_PHYSTS|APP_FCS, DRVINFO sniffer-mode,
RXFLTMAP0/1/2=0xFFFF). RX frames decode via `rx.iter_frames` (24-byte rx_pkt_desc + jaguar2 phy-status
RSSI, FCS-stripped).

Not registered in the manager (the mainline `chips/rtl8822bu/` owns 2357:0138); this `_dkms` port
is exercised standalone via `scripts/chips/rtl8822bu_dkms/test_hw.py`. `inject_frame` is a stub — the TX
descriptor is a later milestone, and the agent never fires live TX.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bringup, chan, dm_watchdog, mac, sipi, tx, txpower
from .rx import iter_frames
from .transport import Rtl8822buTransport

logger = logging.getLogger(__name__)

USB_VID_REALTEK = 0x2357
USB_PID_T3U_PLUS = 0x0138
_DEFAULT_CHANNEL = 1
_HEAL_5G_CHANNEL = 36                           # 5 GHz channel used to re-cycle a stuck cold synth
_BULK_OUT_EP_TX = 0x05                          # 8822b bulk-OUT (FW/TX)
CHANNELS_2G = list(range(1, 15))
CHANNELS_5G = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
               128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
# Scan set excludes the DFS band (52-144): passive-scan-only, radar-shared, home APs avoid it.
# set_channel + verify_channels still drive the full CHANNELS_5G above, byte-for-byte vs the capture.
CHANNELS_5G_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]

# The pcap-gated reference card's EFUSE/chip-cut burn (TP-Link Archer T3U+). A card whose burn
# differs runs ported-but-hardware-untested branches (FEM CCA table / RFE pinmux / SoML RxHP arm),
# so connect() tags it once. rfe_type 3 = iFEM, cut 3 = ODM_CUT_D.
_REF_RFE_TYPE = 3
_REF_CUT = 3
# rfe types whose per-channel RFE PINMUX is NOT ported (OEM-only phydm_8822b_type15/18_rfe); the
# dispatch runs the iFEM pinmux as a give-it-a-shot fallback, and connect() escalates the warning.
_RFE_PINMUX_UNPORTED = frozenset({15, 18})


def _rx_state_line(t) -> str:
    """Read back the band-dependent RX-path registers for the cold-wedge diagnostic.

    Each is decoded with its expected 2.4 GHz value, so a single silent-boot capture is
    interpretable on its own — but the money comparison is the ch1 snapshot from the cold
    (deaf) initial tune vs the ch1 snapshot after a 5->2.4 round-trip (working): whichever
    field differs is the register `switch_band` failed to wire from the cold-init state.
    Reads only; DEBUG-gated by the callers so it adds no USB traffic in a normal run.
    """
    rf18a = sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18)
    rf18b = sipi.read_rf_reg(t, sipi.RF_PATH_B, 0x18)
    cbc = t.read32(0x0CBC)
    ca0 = t.read32(0x0CA0)
    r808 = t.read32(0x0808)
    r8cc = t.read32(0x08CC)
    a9c = t.read32(0x0A9C)
    r454 = t.read8(0x0454)
    ra80 = t.read32(0x0A80)
    igi_a = sipi.get_bb_reg(t, 0x0C50, 0x7F)
    igi_b = sipi.get_bb_reg(t, 0x0E50, 0x7F)
    return (
        f"RF18 A=0x{rf18a:05x} B=0x{rf18b:05x} (ch=low byte; 2.4G: bit8/bit16 clear) | "
        f"ant 0xCBC[9:8]={(cbc >> 8) & 3} (2.4G->2 5G->1) 0xCA0=0x{ca0 & 0xFFFF:04x} (2.4G->0xa501) | "
        f"0x808 cck_en[28]={(r808 >> 28) & 1} (2.4G->1) rx_ant[7:0]=0x{r808 & 0xFF:02x} | "
        f"0x8CC=0x{r8cc:08x} (2.4G->0x08108492) | "
        f"IGI A=0x{igi_a:02x} B=0x{igi_b:02x} | cck_new_agc 0xA9C[17]={(a9c >> 17) & 1} | "
        f"0x454[7]={(r454 >> 7) & 1}(2.4G->0) 0xA80[18]={(ra80 >> 18) & 1}(2.4G->0)"
    )


class Rtl8822buDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G_NON_DFS
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: Rtl8822buTransport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = transport
        self.mac_address: Optional[str] = None
        self._chip = None                       # (info, efuse) from cold_bringup
        # Runtime EFUSE/chip-cut discriminators (set after cold_bringup). Default = the pcap-gated
        # reference card (rfe_type 3 iFEM, D-cut); a non-reference burn re-selects the FEM CCA
        # table + RFE pinmux + SoML RxHP arm in chan.set_channel_bw.
        self._rfe_type: int = _REF_RFE_TYPE
        self._cut: int = _REF_CUT
        self._txpwr_pg = None                   # decoded PG TX-power block (per-channel TXAGC)
        self._channel: Optional[int] = None
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._io_lock = asyncio.Lock()
        self._dig_st: Optional[dm_watchdog.DigState] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        # Per-dwell RX tally (DEBUG only) — proves which channels are deaf when the
        # intermittent cold-boot "2.4 GHz silent until the first 5 GHz hop" wedge hits.
        self._dbg_frames = 0
        self._dbg_beacons = 0

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8822buDkmsDriver":
        return cls(Rtl8822buTransport(dev, bulk_out_ep=_BULK_OUT_EP_TX))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    def _claim(self) -> None:
        """Detach kernel driver / configure / claim interface 0 (OS-level USB plumbing —
        outside the vendor op stream the gate reproduces)."""
        dev = self.transport.dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(dev, 0)

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._claim)

            if progress_cb:
                progress_cb(0.1, "Cold bring-up: chip-ID / EFUSE / FW / MAC / BB / RF")
            info, e = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
            self._chip = (info, e)
            self._rfe_type, self._cut = e.rfe_type, info.chip_ver
            self._txpwr_pg = txpower.parse_pg(e.log_map)
            if e.mac_address:                          # program the card's own MAC (TX source/ACK)
                await loop.run_in_executor(None, mac.set_mac_addr, self.transport, e.mac_address)
                self.mac_address = e.mac_address
            self._log_detected_config(info, e)

            if progress_cb:
                progress_cb(0.8, f"Tuning to channel {_DEFAULT_CHANNEL} @ 20 MHz")

            def _initial_tune(t):
                chan.set_channel_bw(t, _DEFAULT_CHANNEL, txpwr_pg=self._txpwr_pg,
                                    rfe_type=self._rfe_type, cut=self._cut)

            await loop.run_in_executor(None, _initial_tune, self.transport)
            self._channel = _DEFAULT_CHANNEL
            await self._dbg_rx_state(f"post-initial-tune ch{_DEFAULT_CHANNEL}")

            # Start the bulk-IN reader before opening the RX gate (an undrained pipe wedges RX FIFO).
            self._reader = RxReaderThread(
                loop, self._read_once, self._dispatch, name="8822bu-dkms-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e))
            self._reader.start()
            # The airmon monitor RX-enable (gate-verified vs the capture's monitor switch):
            # MSR no-link, RCR=AAP|APP_PHYSTS|APP_FCS, DRVINFO sniffer-mode, RXFLTMAP0/1/2=0xFFFF.
            await loop.run_in_executor(None, mac.enable_monitor, self.transport)
            await loop.run_in_executor(None, self._heal_cold_synth, self.transport)
            await self._dbg_rx_state(f"post-enable-monitor ch{_DEFAULT_CHANNEL}")

            # Seed the DIG state from the chip and start the runtime PHYDM watchdog (~2 s cadence): the
            # dig_init IGI is only a seed, so without this loop the RX gain never tracks the channel's
            # false-alarm rate. Reads FA counters, adapts IGI (0xC50/0xE50), resets the counters.
            def _seed_dig(tr):
                return dm_watchdog.DigState(
                    cur_ig_value=sipi.get_bb_reg(tr, 0x0C50, 0x7F),
                    cck_new_agc=bool(sipi.get_bb_reg(tr, 0x0A9C, 1 << 17)))

            self._dig_st = await loop.run_in_executor(None, _seed_dig, self.transport)
            self._watchdog_task = loop.create_task(self._watchdog_loop())

            if progress_cb:
                progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz (monitor)")
            return True
        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("bring-up", str(e)) from e

    def _log_detected_config(self, info, e) -> None:
        """One-line log of the EFUSE/chip-cut burn at connect. The pcap-gated reference card is
        rfe_type 3 (iFEM) / D-cut / 2T2R; the runtime FEM branches (chan._ccapar_by_rfe FEM CCA
        table, chan._rfe_pinmux, the switch_band SoML RxHP arm) are ported from vendor C but only
        this burn is hardware-verified, so a different burn is tagged. rfe 15/18 (OEM pinmux) is
        not ported and runs the iFEM fallback — called out as a genuinely untested variant."""
        untested = e.rfe_type != _REF_RFE_TYPE or info.chip_ver != _REF_CUT
        logger.info(
            "RTL8822BU board: rfe_type=%d cut=%d rf=2T2R crystal_cap=0x%02x thermal=0x%02x "
            "mac=%s%s", e.rfe_type, info.chip_ver, e.crystal_cap, e.thermal_meter,
            e.mac_address or "<none>", "  [untested variant]" if untested else "")
        if e.rfe_type in _RFE_PINMUX_UNPORTED:
            logger.warning("RTL8822BU: untested variant: rfe_type=%d RFE pinmux "
                           "(phydm_8822b_type%d_rfe) is not ported — running the iFEM fallback; "
                           "antenna routing may be wrong.", e.rfe_type, e.rfe_type)

    async def _watchdog_loop(self) -> None:
        """Run `phydm_watchdog` every ~2 s (the vendor cadence) — read the FA counters, adapt the RX
        IGI, reset the counters. Serialized with `set_channel` via `_io_lock`; control I/O only, never
        802.11 TX. A transient USB hiccup skips the tick rather than killing the loop."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(2.0)
            try:
                async with self._io_lock:
                    await loop.run_in_executor(
                        None, dm_watchdog.phydm_watchdog, self.transport, self._dig_st)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("8822bu watchdog tick skipped: %s", e)

    # --- RX path -----------------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
            # iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is None:
                continue
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                self._dbg_frames += 1                       # per-dwell tally (see set_channel)
                if parsed.type == "beacon":
                    self._dbg_beacons += 1
                cb(parsed)

    async def _enable_rx_acks(self) -> None:
        """No-op: enable_monitor already accept-alls RXFLTMAP1 (all ctrl subtypes incl. ACK are
        admitted), so the recipient's ACK control frames already reach RX. Nothing to enable on
        the chip (the base arms the tally). Not enter_active_monitor, which makes the chip emit ACKs."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    def _heal_cold_synth(self, t) -> None:
        """Recover the intermittent cold-boot 2.4 GHz synth wedge (~20% of cold boots).

        Symptom: the cold->2.4 GHz tune leaves the synth unlocked (RF18 bit15 set) and 2.4 GHz RX
        is deaf until a band re-cycle. The bring-up wire is byte-for-byte (verify_pcap /
        verify_channels green), so this is a HW synth-lock fault the kernel's tight transfer pacing
        avoids and userland USB intermittently hits — not a missing op. The chip's own recovery is a
        5->2.4 GHz re-cycle, but HW-measured it only re-locks once the synth has SETTLED: an immediate
        bounce right after the stuck tune does nothing, a short settle first makes it take. So settle,
        bounce through 5 GHz and back, re-check; repeat a few times. No-op on the 80% of boots that
        lock cleanly (bit15 clear -> returns immediately)."""
        for attempt in range(4):
            if not (sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18) & (1 << 15)):
                return
            logger.warning("8822bu cold 2.4 GHz synth unlocked (RF18 bit15) — settle + re-cycle "
                           "5->2.4 GHz (try %d)", attempt + 1)
            time.sleep(0.3)
            chan.set_channel_bw(t, _HEAL_5G_CHANNEL, prev_ch=_DEFAULT_CHANNEL,
                                txpwr_pg=self._txpwr_pg, rfe_type=self._rfe_type, cut=self._cut)
            chan.set_channel_bw(t, _DEFAULT_CHANNEL, prev_ch=_HEAL_5G_CHANNEL,
                                txpwr_pg=self._txpwr_pg, rfe_type=self._rfe_type, cut=self._cut)
        if sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18) & (1 << 15):
            logger.error("8822bu 2.4 GHz synth still unlocked after re-cycles — RX may be deaf on 2.4 GHz")

    async def _dbg_rx_state(self, ctx: str) -> None:
        """Log the decoded RX-path register read-back (DEBUG only) — the cold-wedge probe."""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            line = await loop.run_in_executor(None, _rx_state_line, self.transport)
        logger.debug("[RXSTATE %s] %s", ctx, line)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        prev = self._channel
        # Report the dwell we're leaving — exposes the "ch1..13 caught 0, ch36 caught N"
        # signature of the intermittent cold-boot 2.4 GHz silence — then reset for the next.
        prev_5g = prev is not None and prev > 14
        band_change = prev is None or prev_5g != (channel > 14)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[HOP] ch%s->%d band_change=%s | ch%s dwell: frames=%d beacons=%d",
                         prev, channel, band_change, prev, self._dbg_frames, self._dbg_beacons)
        self._dbg_frames = self._dbg_beacons = 0

        def _tune(t):
            chan.set_channel_bw(t, channel, prev_ch=prev, txpwr_pg=self._txpwr_pg,
                                rfe_type=self._rfe_type, cut=self._cut)

        async with self._io_lock:
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        if band_change:
            await self._dbg_rx_state(f"post-tune ch{channel} (band change)")
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the fill_fake_txdesc descriptor (`tx.build_inject_txdesc`, HW ACK-retry limit
        12) and bulk-OUT the frame once. Live TX is the user's explicit
        action — the agent never calls this; the descriptor build is unit-tested in test_tx.py (no
        TX in the passive capture to pcap-diff)."""
        payload = tx.build_inject_txdesc(bytes(frame_bytes))
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, self.transport.bulk_out, payload)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets EN_HWSEQ), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Re-point REG_MACID to ``mac`` so the hardware HW-ACKs frames to it.
        Reversed by exit_active_monitor."""
        await self._set_self_mac(":".join(f"{b:02x}" for b in mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in REG_MACID."""
        if self.mac_address:
            await self._set_self_mac(self.mac_address)

    async def _set_self_mac(self, mac_str: str) -> None:
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, mac.set_mac_addr, self.transport, mac_str)

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.release_interface(self.transport.dev, 0)
        except usb.core.USBError as e:
            logger.debug("release_interface(0): %s", e)
        self.transport.close()
