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
byte-diffed against the cold-boot capture by ``scripts/rtl8188eus_dkms/verify_pcap.py``. The
TX path (``tx.py`` + ``inject_frame``) is wired and HW-confirmed (deauth/EAPOL on the air).

Registered in ``wlan/manager.py`` behind ``WIFIT3_RTL8188`` — the mainline-derived
``rtl8188eus`` stays the default for 2357:010c until this vendor port is hardware-proven to
tie/beat it on 2.4 GHz breadth; ``WIFIT3_RTL8188=dkms`` opts in. Exercise via
``scripts/rtl8188eus_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import (
    bb, chan, dig, dm, efuse, firmware, mac, monitor, powertrack, pwrseq, rf, sreset, tx,
    txpower,
)
from .constants import DEFAULT_INIT_CHANNEL, PID, VID
from .rx import iter_frames
from .transport import Rtl8188eusTransport

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8188eufw.bin"
_SCAN_START_CHANNEL = 1   # first channel tuned at connect (scan starts at ch1)


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8188eusDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(VID, PID,
                 "Realtek RTL8188EUS 1T1R (TL-WN722N v2/v3) — vendor/DKMS port"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(chan.CHANNELS_2G)   # 2.4 GHz, 20 MHz
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    def __init__(self, transport: Rtl8188eusTransport):
        self.transport = transport
        self.mac_address: Optional[bytes] = None
        self._channel: Optional[int] = None
        self._tx_power = None              # path-A efuse TX-power info (TxPwr2G)
        self._eeprom_thermal = 0x18        # efuse thermal base (set from efuse in connect)
        self._dm_seed = None               # dm.DmSeed carried from InitHalDm to the watchdog
        self._rf_chnl: int = 0            # RfRegChnlVal[A], stateful across set_channel
        self._rx_cb: Optional[Callable[[dict], None]] = None
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

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()

        if progress_cb:
            progress_cb(0.0, "Power-on + reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, self._power_on_and_read_efuse)
        self.mac_address = params.mac_address
        self._tx_power = params.tx_power
        # Thermal base for the power-track watchdog (efuse EEPROM_THERMAL_METER_88E 0xBA;
        # the 0xff/autoload-fail default is EEPROM_Default_ThermalMeter_88E 0x18).
        raw_thermal = params.efuse_map[0xBA]
        self._eeprom_thermal = 0x18 if raw_thermal == 0xFF else raw_thermal
        logger.info("RTL8188EUS efuse: crystal_cap=0x%02x mac=%s",
                    params.crystal_cap,
                    params.mac_address.hex(":") if params.mac_address else "<none>")

        if progress_cb:
            progress_cb(0.25, "Uploading firmware")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, firmware.download_firmware, self.transport, fw)
        if not ready:
            logger.error("RTL8188EUS firmware download did not reach WINTINI_RDY")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False

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
            loop, self._read_once, self._dispatch, name="8188eus-dkms-rx")
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
        with scripts/rtl8188eus_dkms/verify_pcap.py), then the monitor opmode entry. The
        firmware was already uploaded in connect()."""
        t = self.transport
        mac.phy_mac_config(t)                                   # M2a
        bb.phy_bb_config(t, crystal_cap=params.crystal_cap)     # M2b
        rf.phy_rf_config(t)                                     # M2c
        efuse.iol_efuse_patch(t)                                # M2d
        mac.init_tx_buffer_boundary(t)
        mac.init_llt(t)                                         # M2e
        mac.init_misc02(t)                                      # M3
        self._rf_chnl = rf.read_rf_chnl_val(t)[0]              # M4a (RfRegChnlVal base)
        bb.bb_turn_on_block(t)                                  # M4b
        mac.invalidate_cam_all(t)                               # M4c
        txpower.set_tx_power(t, params.tx_power, DEFAULT_INIT_CHANNEL)  # M5
        mac.init_misc11_tail(t)                                 # M6
        self._dm_seed = dm.init_hal_dm(t)                       # M7 — and carry the DM seed
        dm.init_hal_tail(t)                                     # M8 (power-track + LCK)
        mac.set_macid(t, self.mac_address or b"\x00" * 6)      # HW_VAR_MAC_ADDR (airmon)
        # monitor opmode is entered in connect() AFTER the channel re-tune (wire order).

    def _read_once(self) -> Optional[bytes]:
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz channel at 20 MHz."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            self._rf_chnl = await loop.run_in_executor(
                None, chan.set_channel, self.transport, self._tx_power, self._rf_chnl, channel)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 management frame (e.g. a deauth / WEP replay).

        Builds the management TX descriptor (tx.build_mgmt_txdesc) and sends
        ``[desc | frame]`` on the bulk-OUT pipe (the single EP 0x02, where the MGMT queue
        maps). ``frame_bytes`` is the MPDU without FCS (the HW appends it). BMC is derived
        from addr1's group bit. Serialized via ``_io_lock`` so the frame is never emitted
        mid-retune. TX is explicit-action only (passive-by-default): nothing on the
        scan/connect path calls this. ``use_no_ack`` is accepted for API compatibility
        (the minimal mgmt descriptor uses the HW-default ACK policy)."""
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast) bit
        # The driver copies the frame's 802.11 sequence number into the TX descriptor (the
        # wire shows desc-seq == frame-seqctrl>>4); seqctrl is bytes [22:24] of the MPDU.
        seqnum = ((int.from_bytes(frame_bytes[22:24], "little") >> 4) & 0xFFF
                  if len(frame_bytes) >= 24 else 0)
        desc = tx.build_mgmt_txdesc(len(frame_bytes), bmc=bmc, seqnum=seqnum)
        async with self._io_lock:           # don't TX mid-retune (set_channel)
            await loop.run_in_executor(None, self.transport.bulk_out, desc + frame_bytes)
        return True

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Re-point REG_MACID to ``mac`` so the hardware HW-ACKs frames to it.
        Reversed by exit_active_monitor."""
        await self._set_self_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in REG_MACID."""
        if self.mac_address:
            await self._set_self_mac(self.mac_address)

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
