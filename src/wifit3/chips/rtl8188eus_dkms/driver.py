"""RTL8188EUS driver — vendor (realtek-rtl8188eus DKMS) cleanroom port.

``connect()`` runs the full pcap-verified bring-up — power-on -> EFUSE -> firmware ->
MAC/BB/RF -> efuse-patch -> LLT -> MISC02 -> RfRegChnlVal read -> BB turn-on -> CAM clear
-> TX power -> MISC11 tail -> InitHalDm phydm seed -> hal_init tail (power-track + LCK) ->
enable RX-BAR -> channel tune -> monitor opmode entry — then starts the bulk-IN RX reader
(promiscuous monitor frames + per-frame RSSI) and the 2 s dynamic-check task.

The dynamic-check task mirrors the vendor ``rtw_dynamic_chk_wk_hdl`` (rtw_cmd.c): each fire
runs the silent-reset status poll (``sreset.py``) then the no-link phydm DIG/AGC watchdog
(``dig.py`` — the runtime adaptation of the M7 IGI/CCK/thermal/NHM seed, central to the RX
goal). The whole operational stream (monitor entry, per-hop channel tunes, every tick) is
byte-diffed against the cold-boot capture by ``scripts/chips/rtl8188eus_dkms/verify_pcap.py``. The
TX path (``tx.py`` + ``inject_frame``) is wired and HW-confirmed (deauth/EAPOL on the air).

Registered in ``wlan/discovery.py`` behind ``WIFIT3_RTL8188`` — the mainline-derived
``rtl8188eus`` stays the default for 2357:010c until this vendor port is hardware-proven to
tie/beat it on 2.4 GHz breadth; ``WIFIT3_RTL8188=dkms`` opts in. Exercise via
``scripts/chips/rtl8188eus_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import (
    bb, chan, dig, dm, efuse, firmware, mac, monitor, phy_cond, powertrack, pwrseq, rf,
    sreset, tx, txpower,
)
from .constants import DEFAULT_INIT_CHANNEL
from .rx import iter_frames
from .transport import Rtl8188eusTransport

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8188eufw.bin"
_SCAN_START_CHANNEL = 1   # first channel tuned at connect (scan starts at ch1)


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8188eusDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(chan.CHANNELS_2G)   # 2.4 GHz, 20 MHz
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: Rtl8188eusTransport):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = transport
        self.mac_address: Optional[str] = None    # colon-hex per the Driver ABC
        self._mac_bytes: Optional[bytes] = None    # raw 6 bytes for register writes
        self._channel: Optional[int] = None
        self._tx_power = None              # path-A efuse TX-power info (TxPwr2G)
        self._board = None                 # efuse board options (BoardOptions; set in connect)
        self._eeprom_thermal = 0x18        # efuse thermal base (set from efuse in connect)
        self._dm_seed = None               # dm.DmSeed carried from InitHalDm to the watchdog
        self._rf_chnl: int = 0            # RfRegChnlVal[A], stateful across set_channel
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        # Runtime DIG/AGC watchdog (M12). Toggleable so a fixed-channel A/B can isolate its
        # effect on per-AP reception (scan_hw.py --no-dig).
        self.enable_dig: bool = True
        self._dig_task: Optional["asyncio.Task"] = None
        # Serializes EP0 control batches (DIG watchdog vs set_channel); RX uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8188eusDkmsDriver":
        return cls(Rtl8188eusTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Wrap the blocking bring-up so a USB fault (e.g. the intermittent EFUSE-read ENOENT)
        surfaces as a BringUpError instead of a raw USBError that slips past
        WlanInterface.connect's handler as a hard crash."""
        try:
            return await self._bringup(progress_cb)
        except BringUpError:
            raise
        except Exception as e:   # noqa: BLE001 — USBError/others during bring-up -> clean failure
            raise BringUpError("bring-up", str(e)) from e

    async def _bringup(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()

        if progress_cb:
            progress_cb(0.0, "Power-on + reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, self._power_on_and_read_efuse)
        self._mac_bytes = params.mac_address
        self.mac_address = params.mac_address.hex(":") if params.mac_address else None
        self._tx_power = params.tx_power
        # Thermal base for the power-track watchdog (efuse EEPROM_THERMAL_METER_88E 0xBA;
        # the 0xff/autoload-fail default is EEPROM_Default_ThermalMeter_88E 0x18).
        raw_thermal = params.efuse_map[0xBA]
        self._eeprom_thermal = 0x18 if raw_thermal == 0xFF else raw_thermal
        self._board = params.board
        logger.debug("RTL8188EUS efuse: crystal_cap=0x%02x mac=%s",
                    params.crystal_cap,
                    params.mac_address.hex(":") if params.mac_address else "<none>")
        # Board-option config log (efuse 0xCA). The reference card is internal PA+LNA;
        # an external-PA/LNA burn drives PHY_SetRFEReg_8188E + the board-gated init-table
        # rows and is untested on hardware (only the internal card is pcap-gated).
        b = params.board
        if b.external_pa_2g or b.external_lna_2g:
            logger.info("RTL8188EUS board [untested variant]: external PA=%s LNA=%s "
                        "type_glna=0x%x -> PHY_SetRFEReg + board-gated init tables active",
                        b.external_pa_2g, b.external_lna_2g, b.type_glna)
        else:
            logger.info("RTL8188EUS board: internal PA+LNA (reference config)")

        if progress_cb:
            progress_cb(0.25, "Uploading firmware")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, firmware.download_firmware, self.transport, fw)
        if not ready:
            raise BringUpError("firmware", "download did not reach WINTINI_RDY")

        if progress_cb:
            progress_cb(0.7, "Configuring MAC / BB / RF")
        await loop.run_in_executor(None, self._phy_config, params)

        # The monitor bring-up the vendor driver runs when an interface goes monitor, in
        # wire order: init_hw_mlme_ext (enable RX-BAR, then the channel tune — which also
        # restores the RF/BB to clean RX after InitHalDm's EDCCA LNA search), then
        # hw_var_set_opmode(MONITOR) (RCR/RXFLTMAP2). [WIRE] cap1 ops 2452 (RX-BAR) ->
        # 2454 (channel) -> 2503 (opmode).
        if progress_cb:
            progress_cb(0.9, f"Monitor: RX-BAR + tuning to channel {_SCAN_START_CHANNEL}")
        await loop.run_in_executor(None, monitor.enable_rx_bar, self.transport)
        await self.set_channel(_SCAN_START_CHANNEL)
        await loop.run_in_executor(None, monitor.enter_monitor, self.transport)

        # Start the bulk-IN RX reader: a blocking bulk read posted on a dedicated thread
        # (off the event loop, so the TUI can't starve RX); each aggregated buffer is
        # split into 802.11 frames + RSSI and fanned to the rx callback.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8188eus-dkms-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()

        # M12: the runtime phydm DIG/AGC watchdog — adapt the M7 IGI seed to the live
        # false-alarm rate every ~2 s. RX-side only (reads FA counters, writes RX gain).
        dig_enabled = self.enable_dig and os.environ.get("WIFIT3_RTL8188_DIG") != "off"
        if dig_enabled:
            self._dig_task = loop.create_task(self._dig_watchdog())
        else:
            logger.info("RTL8188EUS DM watchdog disabled (gain frozen at the InitHalDm seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_SCAN_START_CHANNEL} @ 20 MHz")
        return True

    async def _dig_watchdog(self) -> None:
        """Periodic no-link phydm DM watchdog. Serialized with set_channel via _io_lock. The DM
        carries software state (IGI + CCK-PD + thermal) across ticks, seeded from the values
        InitHalDm read — carried in self._dm_seed, NOT re-read (the vendor does not re-read at
        tick-start, so neither do we: no extra wire ops)."""
        loop = asyncio.get_running_loop()
        try:
            seed = self._dm_seed
            state = dig.seed_state(seed.igi, seed.cck_cca)
            pt_state = powertrack.seed_state(
                seed.ofdm_swing_raw, seed.cck_swing_raw, self._eeprom_thermal)
            while True:
                await asyncio.sleep(dig.WATCHDOG_PERIOD_S)
                try:
                    async with self._io_lock:
                        # rtw_dynamic_chk_wk_hdl runs the silent-reset poll then the phydm
                        # watchdog in one 2 s tick (rtw_cmd.c:2737).
                        await loop.run_in_executor(None, sreset.status_check, self.transport)
                        tick = await loop.run_in_executor(
                            None, dig.watchdog_tick, self.transport, state, pt_state)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    # A per-tick fault must skip this tick, NOT end the watchdog. The
                    # deferred IQK/LCK recalibration raises NotImplementedError once the
                    # chip heats past |Δthermal| >= 8 C; ending the loop there freezes the
                    # gain while the RF keeps drifting, collapsing RX sensitivity over a
                    # long run. Keep DIG/CCK-PD adapting; only the deferred work is skipped.
                    logger.debug("RTL8188EUS DIG: tick skipped on fault", exc_info=True)
                    continue
                logger.debug("RTL8188EUS DIG: IGI=0x%02x fa=%d (ofdm=%d cck=%d)",
                             tick.igi, tick.fa_cnt, tick.ofdm_fa, tick.cck_fa)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8188EUS DIG watchdog stopped on error")

    # --- bring-up (blocking; run in an executor) ---------------------------
    def _power_on_and_read_efuse(self):
        """Vendor probe order: power-on, then the IOL efuse read (crystal_cap / MAC /
        TX-power), then the MISC01 queue/page setup."""
        t = self.transport
        efuse.read_adapter_info(t)          # probe: chip-version + autoload + efuse-access ON
        pwrseq.power_on(t)
        params = efuse.read_chip_params(t)
        mac.init_misc01(t)
        return params

    def _phy_config(self, params) -> None:
        """The deterministic init chain after firmware (all pcap-verified — keep in sync
        with scripts/chips/rtl8188eus_dkms/verify_pcap.py), then the monitor opmode entry. The
        firmware was already uploaded in connect()."""
        t = self.transport
        # phydm board/LNA-type driver words gate the board-conditional init-table rows
        # (internal PA+LNA -> the reference walk). Derived from efuse 0xCA.
        dw = phy_cond.build_driver_words(
            params.board.external_lna_2g, params.board.external_pa_2g, params.board.type_glna)
        mac.phy_mac_config(t, dw)                               # M2a
        bb.phy_bb_config(t, crystal_cap=params.crystal_cap, driver_words=dw)   # M2b
        rf.phy_rf_config(t, dw)                                 # M2c
        efuse.iol_efuse_patch(t)                                # M2d
        mac.init_tx_buffer_boundary(t)
        mac.init_llt(t)                                         # M2e
        mac.init_misc02(t)                                      # M3
        self._rf_chnl = rf.read_rf_chnl_val(t)[0]              # M4a (RfRegChnlVal base)
        bb.bb_turn_on_block(t)                                  # M4b
        mac.invalidate_cam_all(t)                               # M4c
        txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL)  # M5
        mac.init_misc11_tail(t)                                 # M6
        bb.phy_set_rfe_reg(t, params.board)                    # PHY_SetRFEReg_8188E (MISC11 tail)
        self._dm_seed = dm.init_hal_dm(t)                       # M7 — and carry the DM seed
        dm.init_hal_tail(t)                                     # M8 (power-track + LCK)
        mac.set_macid(t, self._mac_bytes or b"\x00" * 6)      # HW_VAR_MAC_ADDR (airmon)
        # monitor opmode is entered in connect() AFTER the channel re-tune (wire order).

    def _read_once(self) -> Optional[bytes]:
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies
            # it iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    async def _enable_rx_acks(self) -> None:
        """Register write: admit ACK control frames (RXFLTMAP1 bit13) so the tap can see the
        AP's ACKs to us. The 8188e leaves RXFLTMAP at its default (ACKs filtered), so this bit
        must be opened explicitly (the base arms the tally). Not enter_active_monitor, which
        makes the chip EMIT ACKs."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor.admit_ack_frames, self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (clear RXFLTMAP1 bit13)."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, monitor.drop_ack_frames, self.transport)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz channel at 20 MHz."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            self._rf_chnl = await loop.run_in_executor(
                None, chan.set_channel, self.transport, self._tx_power, self._rf_chnl, channel)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the management TX descriptor (tx.build_mgmt_txdesc, HW ACK-retry limit
        12, the vendor value) and send ``[desc | frame]`` on the bulk-OUT pipe (the
        single EP 0x02, where the MGMT queue maps) once. ``frame_bytes`` is the MPDU without FCS
        (the HW appends it). BMC is derived from addr1's group bit. Serialized via ``_io_lock``
        so the frame is never emitted mid-retune. TX is explicit-action only (passive-by-default):
        nothing on the scan/connect path calls this."""
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast) bit
        # The driver copies the frame's 802.11 sequence number into the TX descriptor (the
        # wire shows desc-seq == frame-seqctrl>>4); seqctrl is bytes [22:24] of the MPDU.
        seqnum = ((int.from_bytes(frame_bytes[22:24], "little") >> 4) & 0xFFF
                  if len(frame_bytes) >= 24 else 0)
        payload = tx.build_mgmt_txdesc(len(frame_bytes), bmc=bmc, seqnum=seqnum) + frame_bytes
        async with self._io_lock:           # don't TX mid-retune (set_channel)
            await loop.run_in_executor(None, self.transport.bulk_out, payload)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets EN_HWSEQ), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID to ``mac`` so the hardware HW-ACKs frames to it.
        Reversed by exit_active_monitor."""
        await self._set_self_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in REG_MACID."""
        if self._mac_bytes:
            await self._set_self_mac(self._mac_bytes)

    async def _set_self_mac(self, mac_bytes: bytes) -> None:
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, mac.set_macid, self.transport, mac_bytes)

    async def close(self) -> None:
        if self._dig_task is not None:
            self._dig_task.cancel()
            try:
                await self._dig_task
            except asyncio.CancelledError:
                pass
            self._dig_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        self.transport.close()
