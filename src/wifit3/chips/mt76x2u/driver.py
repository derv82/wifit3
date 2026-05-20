"""MT76x2U / MT7612U driver — WlanDriver Protocol implementation (M0 scaffold).

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

M0 scope: claim USB interface, read MT_ASIC_VERSION, expose a probe
entrypoint. Bring-up (FW upload / PHY init / RX path) lands in M1..M4.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from .chan import set_channel_20mhz
from .constants import (
    MT_ASIC_VERSION,
    MT_MCU_COM_REG0,
    MT76XX_REV_E3,
    USB_IDS_MT76X2U,
)
from .eeprom import (
    read_block,
    read_chip_id,
    read_mac_address,
    read_nic_conf_0,
    read_nic_conf_1,
)
from .firmware import upload_firmware
from .mac import mac_reset, mac_setaddr, mac_start, mac_stop
from .mcu import McuChannel, mcu_init
from .phy import mcu_load_cr, phy_set_rxpath, phy_set_txdac
from .power import init_dma, power_on, reset_wlan, wait_for_mac, wait_for_wpdma_idle
from .rx import RxDrainer
from .transport import MT76x2UTransport
from .tx import inject_frame as _inject_frame

logger = logging.getLogger(__name__)


class MT76x2UDriver:
    """Driver for MT7612U-family USB cards (Alfa AWUS036ACM, ASUS USB-AC54, ...).

    M0 only does enough to confirm we can talk to the chip; M1 picks up at
    firmware upload.
    """

    SUPPORTED_IDS = [
        DeviceID(vid, pid, desc) for (vid, pid, desc) in USB_IDS_MT76X2U
    ]
    # 2.4 GHz channels 1..13 + non-DFS 5 GHz (UNII-1 + UNII-3).
    # DFS bands (52..144) are PHY-capable on this chip but require radar
    # detection support we won't ship; left out until that lands.
    SUPPORTED_CHANNELS = (
        list(range(1, 14))
        + [36, 40, 44, 48]
        + [149, 153, 157, 161, 165]
    )

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "MT76x2UDriver":
        return cls(dev, id_entry)

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID):
        self.dev = dev
        self.id_entry = id_entry
        self.transport = MT76x2UTransport(dev)
        self.mcu = McuChannel(self.transport)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_drainer: Optional[RxDrainer] = None
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.asic_version: Optional[int] = None
        self.asic_rev: Optional[int] = None
        self.eeprom_chip_id: Optional[int] = None
        self.nic_conf_0: Optional[dict] = None
        self.nic_conf_1: Optional[dict] = None
        self.chainmask: int = 0x0202  # (tx_path << 8) | rx_path — refined from EEPROM
        self.current_channel: int = 6
        self._init_cal_done: bool = False
        self._bt_rcal_valid: bool = True   # refined from EEPROM EE_BT_RCAL_RESULT

    # ---- Discovery / public state ----------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- Lifecycle --------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M0+M1: probe + firmware upload. PHY/RX/TX land in M3+."""
        if progress_cb:
            progress_cb(0.05, "Claiming MT7612U interface")
        try:
            self._claim_interface()
        except usb.core.USBError as e:
            logger.error("MT7612U: failed to claim interface: %s", e)
            return False

        try:
            self.transport.assert_expected_endpoints()
        except RuntimeError as e:
            logger.error(str(e))
            return False

        if progress_cb:
            progress_cb(0.10, "Reading MT_ASIC_VERSION")
        try:
            self.asic_version = self.transport.read32(MT_ASIC_VERSION)
        except usb.core.USBError as e:
            logger.error("MT7612U: ASIC version read failed: %s", e)
            return False

        # Low byte is the revision (E1/E3/E4...). High 16 bits are 0x7612 or
        # 0x7662 depending on the silicon strap.
        self.asic_rev = self.asic_version & 0xFF
        logger.info(
            "MT7612U: ASIC version=0x%08x (rev=0x%02x, %sE3+)",
            self.asic_version, self.asic_rev,
            "" if self.asic_rev >= MT76XX_REV_E3 else "PRE-",
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

        # ----- Cold path: power_on + FW upload + MCU init -----
        if not warm:
            if progress_cb:
                progress_cb(0.15, "WLAN reset + power_on (RF / MTCMOS)")
            reset_wlan(self.transport)
            power_on(self.transport)
            if not await wait_for_mac(self.transport):
                logger.error("MT7612U: MAC never came alive after power_on")
                return False

            if progress_cb:
                progress_cb(0.30, "Uploading firmware (ROM patch + main FW)")
            if not await upload_firmware(self.transport, self.asic_rev):
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

        # ----- EEPROM (idempotent — chip-side EEPROM, FW not required) -----
        if progress_cb:
            progress_cb(0.65, "Reading EEPROM")
        try:
            self.eeprom_chip_id = read_chip_id(self.transport)
            self.mac_address = read_mac_address(self.transport)
            self.nic_conf_0 = read_nic_conf_0(self.transport)
            self.nic_conf_1 = read_nic_conf_1(self.transport)
        except Exception as e:
            logger.error("MT7612U: EEPROM read failed: %s", e)
            return False
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
        logger.info(
            "MT7612U: MAC=%s eeprom_chip=0x%04x chainmask=0x%04x "
            "(rx=%d tx=%d) pa_int_2g=%s lna_ext_2g=%s",
            self.mac_address, self.eeprom_chip_id, self.chainmask,
            rx_path, tx_path,
            self.nic_conf_0["pa_int_2g"],
            self.nic_conf_1["lna_ext_2g"],
        )

        # ----- MAC bring-up for RX -----
        if progress_cb:
            progress_cb(0.75, "MAC reset + initvals + setaddr")
        if not await mac_reset(self.transport):
            return False
        mac_bytes = bytes(int(b, 16) for b in self.mac_address.split(":"))
        mac_setaddr(self.transport, mac_bytes)

        # ----- BBP CR table via MCU -----
        if progress_cb:
            progress_cb(0.82, "MCU LOAD_CR (BBP coefficient table)")
        if not await mcu_load_cr(self.mcu, cr_type=0, temp_level=0, channel=0):
            logger.error("MT7612U: mcu_load_cr failed")
            return False

        # ----- PHY rxpath/txdac (chainmask-dependent BBP toggles) -----
        phy_set_rxpath(self.transport, self.chainmask)
        phy_set_txdac(self.transport, self.chainmask)

        # ----- Channel tune + mac_start + RX drainer -----
        if progress_cb:
            progress_cb(0.90, f"Tuning to ch {self.current_channel}")
        if not await set_channel_20mhz(
            self.transport, self.mcu, self.current_channel,
            self.asic_rev, self.chainmask,
            init_cal_done=self._init_cal_done,
            bt_rcal_valid=self._bt_rcal_valid,
        ):
            logger.error("MT7612U: set_channel(%d) failed", self.current_channel)
            return False
        self._init_cal_done = True

        if progress_cb:
            progress_cb(0.95, "Enabling RX (mac_start)")
        if not await mac_start(self.transport, monitor=True):
            return False

        self._rx_drainer = RxDrainer(
            self.transport,
            frame_callback=self._on_decoded_rx,
        )
        await self._rx_drainer.start()

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

    async def set_channel(self, channel: int) -> bool:
        if channel not in self.SUPPORTED_CHANNELS:
            logger.error("MT7612U: channel %d not in SUPPORTED_CHANNELS", channel)
            return False
        if not await set_channel_20mhz(
            self.transport, self.mcu, channel,
            self.asic_rev, self.chainmask,
            init_cal_done=self._init_cal_done,
            bt_rcal_valid=self._bt_rcal_valid,
        ):
            logger.error("MT7612U: set_channel(%d) failed", channel)
            return False
        self._init_cal_done = True
        self.current_channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        # `use_no_ack=True` (the wifit3 convention) → ack=False on the chip.
        return await _inject_frame(self.transport, frame_bytes, ack=not use_no_ack)

    async def close(self) -> None:
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
        """Activate cfg 1 and claim interface 0.

        WinUSB usually leaves the device pre-configured; on Linux the
        kernel's mt76x2u must be blacklisted first (rmmod / modprobe -r).
        """
        try:
            self.dev.set_configuration(1)
        except usb.core.USBError as e:
            logger.debug("set_configuration(1) on MT7612U: %s (often benign)", e)
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            # Already-claimed is fine on Windows; bubble up on Linux.
            logger.debug("claim_interface(0) on MT7612U: %s", e)
