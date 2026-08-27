"""MT76x2U / MT7612U driver — Driver Protocol implementation (M0 scaffold).

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

M0 scope: claim USB interface, read MT_ASIC_VERSION, expose a probe
entrypoint. Bring-up (FW upload / PHY init / RX path) lands in M1..M4.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from .chan import set_channel_20mhz, phy_channel_calibrate
from .constants import (
    MT_ASIC_VERSION,
    MT76X2_LOW_RSSI_GAIN_THRESH_2G,
    MT76X2_LOW_RSSI_GAIN_THRESH_5G,
    MT76X2_RSSI_GAIN_THRESH_2G,
    MT76X2_RSSI_GAIN_THRESH_5G,
    MT_CALIBRATE_INTERVAL_S,
    MT_MCU_COM_REG0,
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_ACK,
    MT76XX_REV_E3,
)
from .eeprom import (
    has_ext_lna,
    read_block,
    read_chip_id,
    read_mac_address,
    read_nic_conf_0,
    read_nic_conf_1,
    read_power_info,
    read_rate_power,
    read_rx_high_gain_2g,
    read_rx_high_gain_5g,
    tssi_enabled as eeprom_tssi_enabled,
)
from .phy import (
    Mt76x2CalState,
    tssi_compensate,
    update_channel_gain,
)
from .firmware import upload_firmware
from .mac import (
    init_beacon_config,
    mac_cc_reset,
    mac_reset,
    mac_setaddr,
    mac_start,
    mac_stop,
    wait_for_txrx_idle,
)
from .mcu import McuChannel, mcu_init
from .phy import mcu_load_cr, phy_set_rxpath, phy_set_txdac
from .power import (
    force_power_cycle,
    init_dma,
    power_on,
    reset_wlan,
    wait_for_mac,
    wait_for_wpdma_idle,
)
from .rx import RxDrainer, ack_ra as rx_ack_ra
from .transport import MT76x2UTransport
from .tx import inject_frame as tx_inject_frame, stamp_seq_ctrl
from .skey import shared_key_table_clear
from .wcid import wcid_table_clear

logger = logging.getLogger(__name__)


class MT76x2UDriver(Driver):
    """Driver for MT7612U-family USB cards (Alfa AWUS036ACM, ASUS USB-AC54, ...).

    M0 only does enough to confirm we can talk to the chip; M1 picks up at
    firmware upload.
    """

    # 2.4 GHz channels 1..14 + non-DFS 5 GHz (UNII-1 + UNII-3).
    # DFS bands (52..144) are PHY-capable on this chip but require radar
    # detection support we won't ship; left out until that lands.
    SUPPORTED_CHANNELS = (
        list(range(1, 15))
        + [36, 40, 44, 48]
        + [149, 153, 157, 161, 165]
    )
    FAKE_MAC = FakeMacSupport.SPOOFABLE
    LINUX_REPLUG_AFTER_MODPROBE = False   # self-colds: force_power_cycle → cold-equivalent, no replug

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "MT76x2UDriver":
        return cls(dev, id_entry)

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.id_entry = id_entry
        self.transport = MT76x2UTransport(dev)
        self.mcu = McuChannel(self.transport)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_drainer: Optional[RxDrainer] = None
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.asic_version: Optional[int] = None
        self.asic_rev: Optional[int] = None
        # Chip strap discriminator: WiFi-only 0x7612 vs WiFi+BT combo (0x7662/
        # 0x7632/...). Gates the ROM-patch semaphore + COEXCFG0 clear. Refined
        # from the runtime ASIC-version read in connect(); default = reference.
        self.is_mt7612: bool = True
        self.eeprom_chip_id: Optional[int] = None
        self.nic_conf_0: Optional[dict] = None
        self.nic_conf_1: Optional[dict] = None
        self.chainmask: int = 0x0202  # (tx_path << 8) | rx_path — refined from EEPROM
        self.current_channel: int = 6
        self._init_cal_done: bool = False
        self._bt_rcal_valid: bool = True   # refined from EEPROM EE_BT_RCAL_RESULT
        # Per-chain LNA-high-gain offsets from EEPROM (`mt76x2_read_rx_gain`).
        # Populated in connect(); fed to apply_gain_adj on every set_channel.
        self._rx_high_gain_2g: tuple[int, int] = (0, 0)
        # Whether the chip's EEPROM advertises TSSI (drives TSSI init block
        # in set_channel + the periodic tssi_compensate loop).
        self._tssi_enabled: bool = False
        # Calibration state shared with chan.py + the periodic recal task.
        self._cal: Mt76x2CalState = Mt76x2CalState()
        # Periodic recalibration task (kernel's `mt76x2u_phy_calibrate` work).
        self._cal_task: Optional[asyncio.Task] = None
        # True while channel-hopping (scan=True tunes). The periodic cal task
        # defers the heavy per-channel cal until we settle (scan=False).
        self._scanning: bool = False
        # Serialises a channel switch against the background heavy cal — the
        # kernel's mt76.mutex. Stops a re-tune interleaving MCU commands with
        # an in-flight calibration.
        self._cal_lock: asyncio.Lock = asyncio.Lock()
        # Regulatory TX power cap. Kernel reads from cfg80211; wifit3 has no
        # regulatory framework yet — clamp to 60 (= 30 dBm * 2, kernel's
        # post-init default before any country code applies).
        self._txpower_conf: int = 60
        # ``WIFIT3_MT76X2U_SET_TXPOWER=0`` skips the kernel's
        # `mt76x2_phy_set_txpower` per-rate TX_PWR_CFG_0..9 + TX_ALC_CFG_0
        # writes on each channel-tune, leaving the static 0x3a3a3a3a
        # initvals in place. Default ON (the kernel default); the switch exists
        # to isolate per-rate TX power when debugging injection on hardware.
        env_setpwr = os.environ.get(
            "WIFIT3_MT76X2U_SET_TXPOWER", "1"
        ).strip()
        self._set_txpower_enabled: bool = env_setpwr != "0"
        # Incrementing 802.11 seq software-stamped per inject via _stamp_tx_seq (build_txwi sets
        # no NSEQ, so the chip transmits the MPDU's seq_ctrl as-is). The base owns the ACK tally.
        self._tx_seqno: int = 0

    # ---- Discovery / public state ----------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxDrainer's
        RxReaderThread on_fatal; resolved at call time so registration order can't strand it."""
        self._on_lost = cb

    async def _cold_init_chip(
        self, progress_cb: Optional[ProgressCallback] = None
    ) -> bool:
        """The kernel cold-start sequence: WLAN reset, RF/MTCMOS
        power-on, firmware upload, DMA init, MCU init.

        Extracted from connect() so the warm-reattach fallback can re-run
        it when ``mcu_load_cr`` times out (the chip's MCU is wedged from
        the previous session and the warm-skip path can't recover it).
        """
        if progress_cb:
            progress_cb(0.15, "WLAN reset + power_on (RF / MTCMOS)")
        reset_wlan(self.transport)
        power_on(self.transport)
        if not await wait_for_mac(self.transport):
            logger.error("MT7612U: MAC never came alive after power_on")
            return False
        if progress_cb:
            progress_cb(0.30, "Uploading firmware (ROM patch + main FW)")
        if not await upload_firmware(self.transport, self.asic_rev,
                                     is_mt7612=self.is_mt7612):
            logger.error("MT7612U: firmware upload failed")
            return False
        if not await wait_for_mac(self.transport):
            logger.error("MT7612U: MAC not ready post-FW")
            return False
        if not await wait_for_wpdma_idle(self.transport, timeout_ms=100):
            logger.warning("MT7612U: WPDMA never idle (continuing)")
        init_dma(self.transport)
        if progress_cb:
            progress_cb(0.55, "Initializing MCU (function_select + radio on)")
        if not await mcu_init(self.mcu):
            logger.error("MT7612U: MCU init failed")
            return False
        return True

    async def _init_mac_tables(self, mac_bytes: bytes,
                               progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Post-FW MAC bring-up: reset + setaddr + idle gate + WCID/SKEY
        clears + beacon config. Idempotent and cheap (~500 ms), so the
        warm-reattach fallback re-runs it after a forced cold reset."""
        if progress_cb:
            progress_cb(0.75, "MAC reset + initvals + setaddr")
        if not await mac_reset(self.transport, is_mt7612=self.is_mt7612):
            return False
        mac_setaddr(self.transport, mac_bytes)
        if not await wait_for_txrx_idle(self.transport):
            logger.warning(
                "MT7612U: TX+RX did not go idle within 100ms before "
                "WCID/SKEY clears; subsequent inject may be unreliable"
            )
        if progress_cb:
            progress_cb(0.78, "WCID table clear (256 slots)")
        wcid_table_clear(self.transport)
        if progress_cb:
            progress_cb(0.79, "Shared key table clear (64 slots)")
        shared_key_table_clear(self.transport)
        init_beacon_config(self.transport)
        return True

    # ---- Lifecycle --------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M0+M1: probe + firmware upload. PHY/RX/TX land in M3+."""
        if progress_cb:
            progress_cb(0.05, "Claiming MT7612U interface")
        try:
            self._claim_interface()
        except usb.core.USBError as e:
            raise BringUpError("claim", str(e)) from e

        try:
            self.transport.assert_expected_endpoints()
        except RuntimeError as e:
            raise BringUpError("endpoints", str(e)) from e

        if progress_cb:
            progress_cb(0.10, "Reading MT_ASIC_VERSION")
        try:
            self.asic_version = self.transport.read32(MT_ASIC_VERSION)
        except usb.core.USBError as e:
            raise BringUpError("asic-version", str(e)) from e

        # Low byte is the revision (E1/E3/E4...). High 16 bits are 0x7612 or
        # 0x7662 depending on the silicon strap.
        self.asic_rev = self.asic_version & 0xFF
        # [SRC] mt76x2/mt76x2.h:26 — is_mt7612 = mt76_chip == 0x7612, where
        # mt76_chip = rev >> 16 ([SRC] mt76.h:1231). Drives the WiFi-only vs
        # WiFi+BT-combo branches (ROM-patch semaphore, COEXCFG0 clear).
        self.is_mt7612 = (self.asic_version >> 16) == 0x7612
        logger.info(
            "MT7612U: ASIC version=0x%08x (rev=0x%02x, %sE3+, chip=0x%04x, %s)",
            self.asic_version, self.asic_rev,
            "" if self.asic_rev >= MT76XX_REV_E3 else "PRE-",
            self.asic_version >> 16,
            "mt7612 (WiFi-only)" if self.is_mt7612 else "combo (WiFi+BT, rom_protect)",
        )

        # ----- Cold/warm gate -----
        # If FW is already running (a previous process left it in this state),
        # skip the cold-only pre-FW work. The post-FW work (mac_reset, channel,
        # mac_start) is idempotent so we re-run it regardless.
        com_reg = self.transport.read32(MT_MCU_COM_REG0)
        warm = bool(com_reg & 0x3)
        if warm:
            self.is_warm = True
            logger.info(
                "MT7612U: firmware already running (MT_MCU_COM_REG0=0x%08x). "
                "Skipping power_on + FW upload.", com_reg,
            )
            # Drain any stale MCU responses left over from the previous
            # session — the chip's MCU firmware is still running with its
            # own internal seq counter, and any unread response in
            # EP_IN_CMD_RESP causes a seq-mismatch on our first wait_resp
            # command (mcu_load_cr). Symptom: ~1-in-10 warm boots fails
            # with "MCU resp seq mismatch: got=4 want=1" → timeout. See
            # [[idle-gate-before-table-clears]] for the similar shape.
            try:
                drained = await self.mcu.drain_response_queue()
                if drained > 0:
                    logger.info(
                        "MT7612U: drained %d stale MCU response(s) from "
                        "previous session", drained,
                    )
            except Exception as e:
                logger.warning(
                    "MT7612U: MCU response drain failed (continuing): %s", e,
                )

        # ----- Cold path: power_on + FW upload + MCU init -----
        if not warm:
            if not await self._cold_init_chip(progress_cb):
                raise BringUpError("cold-init", "cold chip init failed")

        # ----- EEPROM (idempotent — chip-side EEPROM, FW not required) -----
        if progress_cb:
            progress_cb(0.65, "Reading EEPROM")
        try:
            self.eeprom_chip_id = read_chip_id(self.transport)
            self.mac_address = read_mac_address(self.transport)
            self.nic_conf_0 = read_nic_conf_0(self.transport)
            self.nic_conf_1 = read_nic_conf_1(self.transport)
        except Exception as e:
            raise BringUpError("eeprom", str(e)) from e
        rx_path = self.nic_conf_0["rx_path"]
        tx_path = self.nic_conf_0["tx_path"]
        self.chainmask = ((tx_path & 0xF) << 8) | (rx_path & 0xF)
        # MCU_CAL_R is gated by EE_BT_RCAL_RESULT (0x138, 1 byte). Kernel
        # only fires it if the EFUSE byte is NOT 0xff (i.e., burned).
        # [SRC] mt76x2/usb_phy.c:148
        try:
            bt_rcal_blob = read_block(self.transport, 0x138, 4)
            self._bt_rcal_valid = bt_rcal_blob[0] != 0xFF
        except Exception:
            self._bt_rcal_valid = False
        # 2.4 GHz RX high-LNA gain offsets (kernel reads on every set_channel
        # but the EEPROM value is static, so read once at connect).
        try:
            self._rx_high_gain_2g = read_rx_high_gain_2g(self.transport)
            logger.debug(
                "MT7612U: RX high-gain 2G offsets ch0=%d ch1=%d",
                self._rx_high_gain_2g[0], self._rx_high_gain_2g[1],
            )
        except Exception as e:
            logger.warning("MT7612U: RX high-gain read failed: %s", e)
            self._rx_high_gain_2g = (0, 0)
        # TSSI enable flag from EEPROM. Drives the TSSI init block in
        # set_channel_20mhz + the periodic tssi_compensate loop. Default
        # trusts the EEPROM (kernel behavior); ``WIFIT3_MT76X2U_TSSI=0`` is a
        # kill switch if TSSI ever regresses TX power on this silicon. The
        # EEPROM feeds sane, kernel-faithful slopes/offsets (verified on
        # 0e8d:7612 2026-07-08), so the old "zeroes TX power" suspicion was
        # never borne out at the EEPROM layer. See MT76X2U.md.
        try:
            eeprom_tssi = eeprom_tssi_enabled(self.transport)
        except Exception as e:
            logger.warning("MT7612U: TSSI read failed: %s", e)
            eeprom_tssi = False
        env_tssi = os.environ.get("WIFIT3_MT76X2U_TSSI", "").strip()
        self._tssi_enabled = False if env_tssi == "0" else eeprom_tssi
        logger.debug(
            "MT7612U: TSSI eeprom=%s env=%r → enabled=%s",
            eeprom_tssi, env_tssi, self._tssi_enabled,
        )
        logger.debug(
            "MT7612U: phy_set_txpower gate enabled=%s",
            self._set_txpower_enabled,
        )
        logger.debug(
            "MT7612U: MAC=%s eeprom_chip=0x%04x chainmask=0x%04x "
            "(rx=%d tx=%d) pa_int_2g=%s lna_ext_2g=%s",
            self.mac_address, self.eeprom_chip_id, self.chainmask,
            rx_path, tx_path,
            self.nic_conf_0["pa_int_2g"],
            self.nic_conf_1["lna_ext_2g"],
        )

        # ----- MAC bring-up + per-station table clears -----
        mac_bytes = bytes(int(b, 16) for b in self.mac_address.split(":"))
        if not await self._init_mac_tables(mac_bytes, progress_cb):
            raise BringUpError("mac-tables", "MAC table init failed")

        # ----- BBP CR table via MCU — first wait_resp command -----
        # If we took the warm-skip path, this is the first command we send
        # to the previous session's MCU. On a wedged MCU, it times out
        # (~1-in-10 warm boots — drain helped some but not all). Fall back
        # to a full cold reset so we always succeed.
        if progress_cb:
            progress_cb(0.82, "MCU LOAD_CR (BBP coefficient table)")
        if not await mcu_load_cr(self.mcu, temp_level=0, channel=0):
            if not warm:
                raise BringUpError("mcu-load-cr", "LOAD_CR (BBP table) failed on the cold path")
            # Warm path: force a HARD power cycle + cold init + redo MAC
            # tables + retry. A plain reset_wlan + power_on isn't enough to
            # clear the chip's MCU register state (ROM-patch-applied bit,
            # FCE config) — without an explicit WLAN_EN off-then-on the
            # subsequent FW upload calls `_program_fce` and times out at
            # MT_TX_CPU_FROM_FCE_BASE_PTR (chip's FCE engine still bound
            # to the wedged previous-session state).
            logger.warning(
                "MT7612U: warm-path mcu_load_cr timed out — forcing power "
                "cycle + cold reset + firmware reload"
            )
            self.mcu._seq = 0
            self.is_warm = False
            warm = False
            await force_power_cycle(self.transport)
            if not await self._cold_init_chip(progress_cb):
                raise BringUpError(
                    "cold-init",
                    "cold init failed after a power cycle — the chip is wedged from the "
                    "previous session; please unplug and replug the USB device.",
                )
            if not await self._init_mac_tables(mac_bytes, progress_cb):
                raise BringUpError("mac-tables", "MAC table init failed after a power cycle")
            if not await mcu_load_cr(self.mcu, temp_level=0, channel=0):
                raise BringUpError(
                    "mcu-load-cr",
                    "LOAD_CR (BBP table) failed after a power cycle + cold reset — please "
                    "unplug and replug the USB device.",
                )
            logger.info("MT7612U: power-cycle + cold-reset fallback succeeded")

        # ----- PHY rxpath/txdac (chainmask-dependent BBP toggles) -----
        phy_set_rxpath(self.transport, self.chainmask)
        phy_set_txdac(self.transport, self.chainmask)

        # ----- Channel tune + mac_start + RX drainer -----
        if progress_cb:
            progress_cb(0.90, f"Tuning to ch {self.current_channel}")
        ch = self.current_channel
        rate_power = read_rate_power(self.transport, band_2g=ch < 36)
        power_info = read_power_info(
            self.transport, ch, band_2g=ch < 36,
            tssi_enabled=self._tssi_enabled,
        )
        if not await set_channel_20mhz(
            self.transport, self.mcu, ch,
            self.asic_rev, self.chainmask,
            cal=self._cal,
            rate_power=rate_power,
            power_info=power_info,
            init_cal_done=self._init_cal_done,
            bt_rcal_valid=self._bt_rcal_valid,
            ext_pa=self._ext_pa_for(ch),
            high_gain=self._high_gain_for(ch),
            tssi_enabled_flag=self._tssi_enabled,
            txpower_conf=self._txpower_conf,
            set_txpower_enabled=self._set_txpower_enabled,
        ):
            raise BringUpError("channel-tune", f"set_channel({ch}) failed")
        # Heavy RF cal inline at bring-up — the kernel runs it on a non-scan
        # settle. Safe to block here: connect() runs in a worker thread, not
        # the UI loop. (Runtime tunes defer this to _periodic_calibrate.)
        await phy_channel_calibrate(
            self.transport, self.mcu, ch,
            cal=self._cal, high_gain=self._high_gain_for(ch),
            ext_pa=self._ext_pa_for(ch), tssi_enabled_flag=self._tssi_enabled,
        )
        mac_cc_reset(self.transport)
        self._init_cal_done = True

        if progress_cb:
            progress_cb(0.95, "Enabling RX (mac_start)")
        if not await mac_start(self.transport, monitor=True):
            raise BringUpError("mac-start", "mac_start (RX enable) failed")

        # connect() is idempotent: tear down any prior cal task / RX drainer
        # before (re)starting, so two cal threads or two drainers can never run
        # on this instance. The manager builds a fresh driver per connect today,
        # but this keeps a future reconnect path from reintroducing the bug.
        if self._cal_task is not None:
            self._cal_task.cancel()
            try:
                await self._cal_task
            except asyncio.CancelledError:
                pass
            self._cal_task = None
        if self._rx_drainer is not None:
            await self._rx_drainer.stop()
            self._rx_drainer = None

        self._rx_drainer = RxDrainer(
            self.transport,
            frame_callback=self._on_decoded_rx,
            raw_callback=self._on_raw_rx,
            on_fatal=lambda e: self._on_lost and self._on_lost(e),
        )
        await self._rx_drainer.start()

        # Periodic recalibration task — kernel's `mt76x2u_phy_calibrate`
        # delayed-work scheduled at end of set_channel. [SRC] usb_phy.c:198.
        self._cal_task = asyncio.create_task(self._periodic_calibrate())

        if progress_cb:
            progress_cb(1.0, f"MT7612U RX live on ch {self.current_channel}")
        return True

    def _on_decoded_rx(self, decoded: dict) -> None:
        """Bridge each decoded RX frame to the WlanInterface callback.

        `decoded` comes from `rx.decode_urb` and has `frame_bytes` + `rssi`
        plus metadata flags. We hand the raw 802.11 bytes to
        `WlanFrameParser.parse_80211_frame` so the shape matches what
        WlanInterface expects from every other driver.
        """
        cb = self._rx_callback
        if cb is None:
            return
        parsed = WlanFrameParser.parse_80211_frame(
            decoded["frame_bytes"], decoded["rssi"],
        )
        if parsed is None:
            return
        cb(parsed)

    def _on_raw_rx(self, data: bytes) -> None:
        """Pre-parse tap for TX-ACK detection (RxDrainer raw_callback). ``ack_ra`` confirms the
        bulk-IN URB carries a 10-byte 0xD4 ACK and returns its RA; the base ``record_ack`` tallies
        it when the tally is armed and the RA is a MAC we inject as (record_ack reads the RA at
        frame[4:10], so the 4 leading bytes are immaterial). The parser drops these control frames,
        so the decode path never surfaces them as parsed dicts."""
        ra = rx_ack_ra(data)
        if ra is not None:
            self.record_ack(b"\x00\x00\x00\x00" + ra)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            logger.error("MT7612U: channel %d not in SUPPORTED_CHANNELS", channel)
            return False
        rate_power = read_rate_power(self.transport, band_2g=channel < 36)
        power_info = read_power_info(
            self.transport, channel, band_2g=channel < 36,
            tssi_enabled=self._tssi_enabled,
        )
        # Only the channel switch + light cals here — fast. The heavy per-channel
        # RF cal is NOT run inline: on a hop (scan=True) the kernel skips it
        # outright ([SRC] usb_phy.c:170); on a settle the periodic cal task runs
        # it in the background, so Focus entry isn't blocked for ~2 s. The lock
        # is the kernel's mt76.mutex — a switch never interleaves with a cal.
        async with self._cal_lock:
            if not await set_channel_20mhz(
                self.transport, self.mcu, channel,
                self.asic_rev, self.chainmask,
                cal=self._cal,
                rate_power=rate_power,
                power_info=power_info,
                init_cal_done=self._init_cal_done,
                bt_rcal_valid=self._bt_rcal_valid,
                ext_pa=self._ext_pa_for(channel),
                high_gain=self._high_gain_for(channel),
                tssi_enabled_flag=self._tssi_enabled,
                txpower_conf=self._txpower_conf,
                set_txpower_enabled=self._set_txpower_enabled,
                scan=scan,
            ):
                logger.error("MT7612U: set_channel(%d) failed", channel)
                return False
            # `mac_cc_reset` follows phy_set_channel in the kernel.
            # [SRC] mt76x2/usb_main.c:44.
            mac_cc_reset(self.transport)
            self._init_cal_done = True
            self.current_channel = channel
            self._scanning = scan
            # A fresh tune invalidates the heavy per-channel cal; the periodic
            # task re-runs it once we've settled (scan=False). [SRC] usb_phy.c:90.
            self._cal.channel_cal_done = False
        return True

    def _ext_pa_for(self, channel: int) -> bool:
        """External-PA enable for ``channel`` band, derived from EEPROM.
        Mirrors `mt76x02_ext_pa_enabled` ([SRC] mt76x02_eeprom.c:91):
        ext-PA enabled iff the internal-PA EEPROM bit is clear."""
        if self.nic_conf_0 is None:
            return False
        if channel >= 36:
            return not self.nic_conf_0.get("pa_int_5g", True)
        return not self.nic_conf_0.get("pa_int_2g", True)

    def _high_gain_for(self, channel: int) -> tuple[int, int]:
        """Per-chain LNA-high-gain offsets for the target band. Read at
        connect for 2.4 GHz (static), but 5 GHz is channel-group-dependent
        so we re-read per channel."""
        if channel >= 36:
            try:
                return read_rx_high_gain_5g(self.transport, channel)
            except Exception as e:
                logger.warning(
                    "MT7612U: 5 GHz RX high-gain read for ch %d failed: %s",
                    channel, e,
                )
                return (0, 0)
        return self._rx_high_gain_2g

    async def _periodic_calibrate(self) -> None:
        """Kernel's `mt76x2u_phy_calibrate` work — re-runs every
        MT_CALIBRATE_INTERVAL (~1 s). Fires `update_channel_gain` (adaptive
        RX gain) + `tssi_compensate` (thermal drift TX comp). Without this,
        long-running sessions drift away from optimal RX/TX even though
        channel-tune set everything correctly initially.
        """
        try:
            while True:
                await asyncio.sleep(MT_CALIBRATE_INTERVAL_S)
                ch = self.current_channel
                band_2g = ch < 36
                # Deferred heavy per-channel cal — kernel's cal_work runs
                # channel_calibrate, self-gated on channel_cal_done ([SRC]
                # usb_phy.c:16,50). Only once settled (not hopping): a hop would
                # discard it on the next tune. Lock = mt76.mutex, so the cal
                # never interleaves with a concurrent re-tune.
                if not self._scanning and not self._cal.channel_cal_done:
                    async with self._cal_lock:
                        try:
                            await phy_channel_calibrate(
                                self.transport, self.mcu, ch,
                                cal=self._cal,
                                high_gain=self._high_gain_for(ch),
                                ext_pa=self._ext_pa_for(ch),
                                tssi_enabled_flag=self._tssi_enabled,
                            )
                        except Exception as e:
                            logger.debug(
                                "MT7612U: channel_calibrate tick: %s", e)
                try:
                    update_channel_gain(
                        self.transport, self._cal,
                        band_2g=band_2g,
                        bw_40plus=False,
                        has_ext_lna=has_ext_lna(self.transport, band_2g),
                        rssi_thresh=(
                            MT76X2_RSSI_GAIN_THRESH_2G if band_2g
                            else MT76X2_RSSI_GAIN_THRESH_5G
                        ),
                        low_rssi_thresh=(
                            MT76X2_LOW_RSSI_GAIN_THRESH_2G if band_2g
                            else MT76X2_LOW_RSSI_GAIN_THRESH_5G
                        ),
                        avg_rssi_all=-75,
                    )
                except Exception as e:
                    logger.debug("MT7612U: update_channel_gain tick: %s", e)
                try:
                    if self._tssi_enabled:
                        power_info = read_power_info(
                            self.transport, ch, band_2g=band_2g,
                            tssi_enabled=True,
                        )
                        await tssi_compensate(
                            self.transport, self.mcu, self._cal,
                            power_info=power_info,
                            band_2g=band_2g,
                            ext_pa=self._ext_pa_for(ch),
                            channel=ch,
                        )
                except Exception as e:
                    logger.debug("MT7612U: tssi_compensate tick: %s", e)
        except asyncio.CancelledError:
            pass

    def _set_ack_admit(self, admit: bool) -> int:
        """RMW MT_RX_FILTR_CFG bit 10 (MT_RX_FILTR_CFG_ACK): clear to admit the AP's
        link-layer ACKs into monitor RX, set to restore the drop. Returns the new value."""
        cur = self.transport.read32(MT_RX_FILTR_CFG)
        new = cur & ~MT_RX_FILTR_CFG_ACK if admit else cur | MT_RX_FILTR_CFG_ACK
        if new != cur:
            self.transport.write32(MT_RX_FILTR_CFG, new)
        return new

    async def _enable_rx_acks(self) -> None:
        """Admit the AP's link-layer ACK control frames (FC=0xD4) into monitor RX by clearing
        MT_RX_FILTR_CFG bit 10 (MT_RX_FILTR_CFG_ACK), so the tap can see ACKs to our injected
        MAC. The base arms the tally; this hook is only the chip register write. (Not active
        monitor, which makes the chip EMIT ACKs.)"""
        async with self._cal_lock:                  # kernel mt76.mutex: no interleave with a tune
            new = self._set_ack_admit(True)
        logger.info("MT7612U RX-ACK admit ON (RX_FILTR_CFG=0x%08x, ACK bit clear)", new)

    async def _disable_rx_acks(self) -> None:
        """Restore the default monitor RX filter (re-set MT_RX_FILTR_CFG_ACK), matching
        ``_enable_rx_acks``."""
        async with self._cal_lock:
            self._set_ack_admit(False)

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the mt76x02 TXINFO + TXWI for ``frame_bytes`` and bulk-OUT it once over EP 0x07
        (AC_VO). The frame always requests ACK, so the chip's own HW retry (limit set globally in
        TX_RTY_CFG at connect) retransmits an un-ACKed unicast. The tuned channel selects the TXWI
        rate band (OFDM on 5 GHz, CCK on 2.4 GHz). The sequence number is already stamped by
        ``_stamp_tx_seq`` (the base calls it before this)."""
        return await tx_inject_frame(self.transport, frame_bytes,
                                     ack=True, channel=self.current_channel)

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Software-stamp an incrementing 802.11 sequence number into seq_ctrl. The 20-byte txwi
        carries no sequence field and build_txwi sets no NSEQ, so the mt76x02 chip transmits the
        MPDU's seq_ctrl as-is; without this every inject reuses seq 0 and the AP dedups a
        multi-frame run as retransmissions. The base calls this once before ``_inject_frame``."""
        buf = bytearray(frame_bytes)
        self._tx_seqno = stamp_seq_ctrl(buf, self._tx_seqno)
        return bytes(buf)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Re-point the self-MAC to ``mac`` so the autoresponder HW-ACKs frames to it
        (mac_setaddr bakes in U2ME_MASK=0xff). RX stays promiscuous; reversed by
        exit_active_monitor."""
        async with self._cal_lock:
            mac_setaddr(self.transport, bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC (autoresponder back to ACKing only it)."""
        if not self.mac_address:
            return
        real = bytes(int(b, 16) for b in self.mac_address.split(":"))
        async with self._cal_lock:
            mac_setaddr(self.transport, real)

    async def close(self) -> None:
        if self._cal_task is not None:
            self._cal_task.cancel()
            try:
                await self._cal_task
            except asyncio.CancelledError:
                pass
            self._cal_task = None
        if self._rx_drainer is not None:
            await self._rx_drainer.stop()
            self._rx_drainer = None
        try:
            await mac_stop(self.transport)
        except Exception as e:
            logger.debug("MT7612U: mac_stop on close ignored: %s", e)
        try:
            usb.util.release_interface(self.dev, 0)
        except Exception as e:
            logger.debug("MT7612U: release_interface ignored: %s", e)
        try:
            usb.util.dispose_resources(self.dev)
        except Exception as e:
            logger.debug("MT7612U: dispose_resources ignored: %s", e)

    # ---- Internal helpers -------------------------------------------------
    def _claim_interface(self) -> None:
        """Detach the kernel driver (Linux), activate cfg 1, claim interface 0.

        On Linux the kernel ``mt76x2u`` auto-binds the card and must be released
        before we can drive it; on Windows + WinUSB there is no kernel driver, so
        the detach is a no-op.
        """
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                logger.info("MT7612U: detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("MT7612U: kernel-driver detach skipped: %s", e)
        try:
            self.dev.set_configuration(1)
        except usb.core.USBError as e:
            logger.debug("set_configuration(1) on MT7612U: %s (often benign)", e)
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            # Already-claimed is fine on Windows; bubble up on Linux.
            logger.debug("claim_interface(0) on MT7612U: %s", e)
