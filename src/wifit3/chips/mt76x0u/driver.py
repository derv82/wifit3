"""MT76x0U / MT7610U driver — WlanDriver Protocol implementation (M1).

Ported from Linux mt76 (kernel v6.18) for wifit3, 2026.

M1 scope: claim USB interface, upload mt7610e.bin firmware, ack FW_READY.
PHY init / RX / TX land in M2..M4.

Per [[feedback_prefer_fork_over_base]] this is a fresh sibling of
chips/mt76x2u/driver.py — same family, no shared imports.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError

from .constants import (
    EP_IN_PKT_RX,
    MT_MAC_STATUS,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_RX,
    MT_MAC_SYS_CTRL_ENABLE_TX,
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_ACK,
    USB_IDS_MT76X0U,
)
from .eeprom import EEPROMError, EFUSEFullInfo, read_efuse_full
from .firmware import FirmwareError, FirmwareUploader
from .mac import (
    MACInitError,
    clear_shared_keys,
    clear_wcids,
    init_mac_registers,
    mac_setaddr,
    wait_for_txrx_idle,
    wait_for_wpdma,
)
from .mcu import MCUChannel, MCUError, mcu_init_smoke_test
from .phy import PHYInitError, init_bbp, phy_init, set_channel_20mhz
from . import rx as rx_mod
from .transport import MT76x0UTransport
from .wire_log import WIRE_LOG

logger = logging.getLogger(__name__)

# FW lives next to this module in assets/.
ASSETS_DIR = Path(__file__).parent / "assets"
FW_FILE_PRIMARY = ASSETS_DIR / "mt7610e_linux-firmware.bin"
FW_FILE_FALLBACK = ASSETS_DIR / "mt7610u_linux-firmware.bin"


class MT76x0UDriver:
    """Driver for MT7610U-family USB cards (e.g. Alfa AWUS036ACHM). WIRE-verified on 0e8d:7610."""

    SUPPORTED_IDS = [
        DeviceID(vid, pid, desc) for (vid, pid, desc) in USB_IDS_MT76X0U
    ]

    # Same channel-set assumption as mt76x2u: 2.4 GHz 1..13 + non-DFS 5 GHz.
    # The MT7610U is single-stream (1T1R) but covers both bands. Refine when
    # M2 channel tuning lands; for now the list only matters for the UI.
    SUPPORTED_CHANNELS = (
        list(range(1, 14))
        + [36, 40, 44, 48]
        + [149, 153, 157, 161, 165]
    )
    FAKE_MAC = FakeMacSupport.SPOOFABLE
    LINUX_REPLUG_AFTER_MODPROBE = False   # self-colds: modprobe -r cold-re-enumerates the card

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "MT76x0UDriver":
        return cls(dev, id_entry)

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID):
        self.dev = dev
        self.id_entry = id_entry
        self.transport = MT76x0UTransport(dev)
        self.mcu = MCUChannel(self.transport)
        # Serializes control-endpoint hardware ops across executor threads.
        # set_channel runs in a run_in_executor thread; when a channel-hop tune
        # is cancelled mid-flight (UI view switch → Focus), asyncio cancels the
        # coroutine but cannot cancel its still-running executor thread. A new
        # set_channel's thread would then interleave RF/BBP register batches
        # with the orphan on the same device and land on a corrupt channel.
        # Held by _set_channel_sync so the second tune waits for the first.
        self._hw_lock = threading.Lock()
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        # Runtime silicon strap: mt76_chip = ASIC_VERSION >> 16 (0x7610 WiFi-only /
        # 0x7630 combo-2.4G+BT / 0x7650 dual). Read in _connect_init_mac; defaults
        # to the captured reference (0x7650, is_mt7630=False) so unread == reference.
        self.chip_id: Optional[int] = None
        self.is_mt7630: bool = False
        # Archer T1U USB `driver_info = 1` quirk → mask out 2 GHz. Keyed on the
        # matched USB id, not EEPROM. [SRC] mt76x0/usb.c:245-246 + usb.c:35.
        self.no_2ghz: bool = (id_entry.vid, id_entry.pid) == (0x2357, 0x0105)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        # M1 + M2 + M3a + M3b + M3c + M3d results, populated by connect().
        self.fw_info: Optional[dict] = None
        self.efuse_full: Optional[EFUSEFullInfo] = None
        self.mcu_smoke: Optional[dict] = None
        self.mac_status_after_init: Optional[int] = None
        self.bbp_version: Optional[int] = None
        self.rxfilter_default: Optional[int] = None
        self.wlan_fun_ctrl_after_ant: Optional[int] = None
        self.coexcfg3_after_ant: Optional[int] = None
        self.bbp_agc0_after_phy: Optional[int] = None
        self.bbp_txbe5_after_phy: Optional[int] = None
        self.rf_b0_r22_after_phy: Optional[int] = None
        # M4a.1 result from set_channel().
        self.current_channel: Optional[int] = None
        self.last_set_channel_state: Optional[dict] = None
        # M6 — WlanInterface RX hook + background drainer (set in connect()).
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_drainer = None   # rx.RxDrainer | None — typed late to dodge circulars
        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._all_acks_seen: int = 0
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK
        self._tx_frames: int = 0
        self._tx_unacked: int = 0

    # ---- Hooks --------------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        """WlanInterface hook (Protocol). Stored and used by the RxDrainer
        background task started in `connect()`."""
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxDrainer's
        RxReaderThread on_fatal; resolved at call time so registration order can't strand it."""
        self._on_lost = cb

    def _on_decoded_rx(self, parsed: dict) -> None:
        """Bridge: each parsed frame from the RxDrainer → the WlanInterface
        callback (if any)."""
        cb = self._rx_callback
        if cb is not None:
            cb(parsed)

    def _on_raw_rx(self, data: bytes) -> None:
        """Pre-parse tap for TX-ACK detection (RxDrainer raw_callback). A 10-byte
        0xD4 MPDU is an 802.11 ACK; RA is the STA the AP ACKed. When armed, keep
        only ACKs to a MAC we inject as. decode_rx_packet drops these short control
        frames later, so this is the only place they're seen."""
        if not self._ack_detect_on:
            return
        ra = rx_mod.ack_ra(data)
        if ra is None:
            return
        self._all_acks_seen += 1
        if ra in self._our_tx_macs:
            self._ack_sightings[ra.hex()] = self._ack_sightings.get(ra.hex(), 0) + 1
            self._ack_last_ts[ra] = time.monotonic()   # for inject wait-for-ack

    # ---- Lifecycle ----------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M1: USB reset + claim + FW upload to FW_READY, then post-FW MAC/PHY bring-up.

        Each blocking phase runs in a worker thread (``asyncio.to_thread``) so the bring-up
        never freezes the Textual event loop; progress is reported on the loop thread at the
        chunk boundaries. The RX drainer wires into the loop, so it stays here (not offloaded).
        """
        WIRE_LOG.marker("begin connect")

        def _p(pct: float, msg: str) -> None:
            line = f"mt76x0u: {msg}"
            if progress_cb:
                progress_cb(pct, line)
            logger.info(line)

        _p(0.02, "Resetting & Claiming device…")
        fw_file = await asyncio.to_thread(self._connect_reset_claim)
        if fw_file is None:
            raise BringUpError("reset/claim", "USB reset/claim failed, or the firmware blob is missing")

        _p(0.06, "Uploading Firmware…")
        if not await asyncio.to_thread(self._connect_upload_fw, fw_file):
            raise BringUpError("firmware", "firmware upload failed")

        _p(0.45, "Initializing MAC…")
        if not await asyncio.to_thread(self._connect_init_mac):
            raise BringUpError("mac", "MAC init failed")

        _p(0.66, "Clearing tables")
        if not await asyncio.to_thread(self._connect_clear_tables):
            raise BringUpError("tables", "per-station table clear failed")

        _p(0.84, "Initing & Calibrating PHY…")
        if not await asyncio.to_thread(self._connect_init_phy):
            raise BringUpError("phy", "PHY init/calibration failed")

        # ----- Background RX drainer (now that TRX is fully live) -----
        from .rx import RxDrainer
        self._rx_drainer = RxDrainer(
            self.transport, frame_callback=self._on_decoded_rx,
            raw_callback=self._on_raw_rx,
            on_fatal=lambda e: self._on_lost and self._on_lost(e),
        )
        await self._rx_drainer.start()

        WIRE_LOG.marker("end connect")
        return True

    def _connect_reset_claim(self):
        """Chunk 1: USB reset + claim + locate FW file. Returns the FW path, or None."""
        # ---- usb_reset_device equivalent. [SRC] mt76x0/usb.c:249
        # The kernel probe path does this BEFORE any chip access. On Linux
        # the implicit usb open often triggers a similar reset; on Windows +
        # WinUSB it doesn't, so we must call it explicitly. Without this the
        # first vendor write after mt76x02u_mcu_fw_reset stalls — the chip's
        # MCU enters a state it never properly recovers from.
        try:
            self.dev.reset()
            logger.info("MT7610U: dev.reset() OK")
        except usb.core.USBError as e:
            # On some platforms / kernels the reset returns ENODEV briefly as
            # the device re-enumerates. Log and continue — if the chip is
            # truly gone, the next claim will fail loudly.
            logger.warning("MT7610U: dev.reset() raised %s (continuing)", e)

        try:
            self.transport.claim()
        except RuntimeError as e:
            logger.error("MT7610U: %s", e)
            return None

        # Locate FW file. Prefer mt7610e (WIRE-verified); fall back to mt7610u
        # only if mt7610e is missing. Refuse to start without either.
        fw_file = FW_FILE_PRIMARY
        if not fw_file.exists():
            if FW_FILE_FALLBACK.exists():
                logger.warning(
                    "MT7610U: %s missing, falling back to %s "
                    "(WIRE-verified blob is the mt7610e variant — verify before relying)",
                    fw_file.name, FW_FILE_FALLBACK.name,
                )
                fw_file = FW_FILE_FALLBACK
            else:
                logger.error("MT7610U: no FW file in %s", ASSETS_DIR)
                return None
        return fw_file

    def _connect_upload_fw(self, fw_file):
        """Chunk 2: upload firmware to FW_READY (force-reset + re-upload if warm)."""
        self._uploader = FirmwareUploader(
            self.transport,
            progress_cb=None,
        )
        try:
            result = self._uploader.load_firmware(fw_file)
        except FirmwareError as e:
            logger.error("MT7610U FW upload failed: %s", e)
            return False
        except usb.core.USBError as e:
            logger.error("MT7610U: USB error during FW upload: %s", e)
            return False

        self.fw_info = result
        self.is_warm = result.get("was_warm", False)
        h = result["header"]
        if self.is_warm:
            logger.info(
                "MT7610U: warm chip detected — force-reset + re-upload OK. "
                "FW v%s build 0x%04x (%s) — ready after %d poll(s)",
                h["fw_ver_str"], h["build_ver"], h["build_time"], result["polls"],
            )
        else:
            logger.info(
                "MT7610U: FW v%s build 0x%04x (%s) — ready after %d poll(s)",
                h["fw_ver_str"], h["build_ver"], h["build_time"], result["polls"],
            )
        return True

    def _connect_init_mac(self):
        """Chunk 3: post-FW MAC/BBP init (init_usb_dma .. init_bbp + RX_FILTR cache)."""
        # Post-FW driver flow mirrors mt76x0u_init_hardware (mt76x0/usb.c:151)
        # which after mcu_init does:
        #   init_usb_dma → wait_for_wpdma → wait_for_mac → reset_csr_bbp →
        #   Q_SELECT → init_mac_registers → wait_for_txrx_idle → init_bbp → ...
        # M1 covered through mcu_init. M2 added init_usb_dma + reset_csr_bbp +
        # Q_SELECT (out of strict kernel order; works because USB chip has no
        # WPDMA busy state to wait on). M3a inserts the missing waits + the
        # MAC reg init + wait_for_txrx_idle.

        # ---- Post-FW step 6: init_usb_dma (kernel mt76x0_init_usb_dma).
        try:
            self._uploader.init_usb_dma()
        except usb.core.USBError as e:
            logger.error("MT7610U: init_usb_dma failed: %s", e)
            return False

        # ---- M3a step 7: wait_for_wpdma. [SRC] mt76x02_dma.h:54-60,
        # [SRC] mt76x0/init.c:175. Returns immediately on USB (no WPDMA busy).
        if not wait_for_wpdma(self.transport):
            logger.error("MT7610U: wait_for_wpdma timed out")
            return False

        # ---- M3a step 8: wait_for_mac (second time, post-FW upload).
        # Kernel does this in init_hardware:179. Already done once during M1
        # (after chip_onoff), but the kernel re-checks here too — ported as-is.
        try:
            self._uploader.wait_for_mac()
        except FirmwareError as e:
            logger.error("MT7610U: wait_for_mac (post-FW) failed: %s", e)
            return False

        # ---- Post-FW step 9: reset_csr_bbp [SRC] mt76x0/init.c:182.
        try:
            self._uploader.reset_csr_bbp()
        except usb.core.USBError as e:
            logger.error("MT7610U: reset_csr_bbp failed: %s", e)
            return False

        # ---- Post-FW step 10: Q_SELECT [SRC] mt76x0/init.c:183 —
        # `mt76x02_mcu_function_select(dev, Q_SELECT, 1)`.
        # [WIRE] capture-2.pcap:423 payload `01000000 01000000`.
        try:
            from .constants import Q_SELECT
            self.mcu.function_select(Q_SELECT, 1)
        except (MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: MCU Q_SELECT failed: %s", e)
            return False

        # ---- M2 diagnostic: MCU smoke-test ----------------------------
        # Verifies the MCU command channel before we drive the init tables
        # through it. Kept from M2 — not strictly in kernel flow but cheap.
        try:
            self.mcu_smoke = mcu_init_smoke_test(self.mcu, self.transport)
            if not self.mcu_smoke["match"]:
                logger.error(
                    "MT7610U: MCU smoke test mismatch (direct=0x%08x vs mcu=0x%08x)",
                    self.mcu_smoke["via_vendor_read"], self.mcu_smoke["via_mcu_read"],
                )
                return False
            logger.info(
                "MT7610U: MCU CMD_RANDOM_READ round-trip OK "
                "(MAC_CSR0 via MCU = 0x%08x)", self.mcu_smoke["via_mcu_read"],
            )
            # ---- Runtime chip strap. mt76_chip = mt76_rr(MT_ASIC_VERSION) >> 16
            # [SRC] mt76x0/usb.c:266 + mt76.h:1231. is_mt7630 (combo 2.4G+BT die)
            # gates: eeprom has_5ghz mask, RF(5,2) patch value, phy_calibrate skip.
            # The captured reference reads 0x7650 → is_mt7630 stays False → its
            # every downstream wire op is byte-identical. This read is absent from
            # the verify_pcap cursor (which drives functions, not connect()).
            from .constants import MT_ASIC_VERSION
            asic_version = self.transport.read32(MT_ASIC_VERSION)
            self.chip_id = (asic_version >> 16) & 0xFFFF
            self.is_mt7630 = self.chip_id == 0x7630
            logger.info(
                "MT7610U: ASIC_VERSION=0x%08x → chip=0x%04x (%s)",
                asic_version, self.chip_id,
                "mt7630 combo (2.4G+BT, no 5GHz)" if self.is_mt7630 else "mt7610/7650",
            )
        except (MCUError, usb.core.USBError) as e:
            # Even after the warm-boot force-reupload, MCU smoke can fail
            # if the chip's MAC/RX-DMA is so wedged that a USB reset +
            # full FW reload can't clear it. Surface an actionable replug
            # message — that's the only known recovery path.
            logger.error(
                "MT7610U: MCU smoke test failed: %s\n"
                "  Chip appears wedged beyond what FW re-upload can fix.\n"
                "  -> Please UNPLUG the dongle, wait ~3 seconds, REPLUG, "
                "and retry.", e,
            )
            return False

        # ---- M3a step 11: init_mac_registers [SRC] mt76x0/init.c:187.
        # Uploads common_mac_reg_table + mt76x0_mac_reg_table via MCU, then
        # 4 direct register tweaks (release MAC reset, EXT_CCA_CFG, FCE_L2_STUFF,
        # WMM_CTRL).
        try:
            init_mac_registers(self.transport, self.mcu)
        except (MCUError, MACInitError, usb.core.USBError) as e:
            logger.error("MT7610U: init_mac_registers failed: %s", e)
            return False

        # ---- M3a step 12: wait_for_txrx_idle [SRC] mt76x0/init.c:189.
        if not wait_for_txrx_idle(self.transport):
            logger.error("MT7610U: wait_for_txrx_idle timed out")
            return False
        self.mac_status_after_init = self.transport.read32(MT_MAC_STATUS)
        logger.info("MT7610U: MAC_STATUS after init = 0x%08x (TX|RX idle)",
                    self.mac_status_after_init)

        # ---- M3b: init_bbp [SRC] mt76x0/init.c:192.
        # phy_wait_bbp_ready, then bbp_init_tab (58 pairs MCU), then 20
        # filtered switch_tab entries direct-write, then dcoc_tab (9 pairs MCU).
        try:
            self.bbp_version = init_bbp(self.transport, self.mcu)
        except (PHYInitError, MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: init_bbp failed: %s", e)
            return False
        logger.info("MT7610U: BBP version = 0x%08x", self.bbp_version)

        # ---- M3c step 13: cache RX_FILTR_CFG. [SRC] mt76x0/init.c:196 —
        # `dev->mt76.rxfilter = mt76_rr(dev, MT_RX_FILTR_CFG);`
        try:
            self.rxfilter_default = self.transport.read32(MT_RX_FILTR_CFG)
            logger.info("MT7610U: RX_FILTR_CFG default = 0x%08x",
                        self.rxfilter_default)
        except usb.core.USBError as e:
            logger.error("MT7610U: RX_FILTR_CFG read failed: %s", e)
            return False
        return True

    def _connect_clear_tables(self):
        """Chunk 4: clear shared keys + WCIDs, read EEPROM, set MAC address."""
        # ---- M3c step 14: clear all 16x4 shared keys.
        # [SRC] mt76x0/init.c:198-200.
        try:
            clear_shared_keys(self.transport)
        except usb.core.USBError as e:
            logger.error("MT7610U: clear_shared_keys failed: %s", e)
            return False

        # ---- M3c step 15: clear all 256 WCIDs.
        # [SRC] mt76x0/init.c:202-203.
        try:
            clear_wcids(self.transport)
        except usb.core.USBError as e:
            logger.error("MT7610U: clear_wcids failed: %s", e)
            return False

        # ---- M3c step 16: full eeprom_init.
        # [SRC] mt76x0/init.c:205 + mt76x0/eeprom.c:312-353.
        try:
            self.efuse_full = read_efuse_full(
                self.transport, is_mt7630=self.is_mt7630, no_2ghz=self.no_2ghz,
            )
        except (EEPROMError, usb.core.USBError) as e:
            logger.error("MT7610U: eeprom_init failed: %s", e)
            return False
        self.mac_address = self.efuse_full.mac_address
        logger.info(
            "MT7610U EFUSE: chip_id=0x%04x ver=0x%02x fae=0x%02x  MAC=%s  "
            "tx=%d rx=%d  bands=%s%s  freq_off=%d  temp_off=%d  "
            "nic0=0x%04x nic1=0x%04x",
            self.efuse_full.chip_id, self.efuse_full.version,
            self.efuse_full.fae, self.efuse_full.mac_address,
            self.efuse_full.tx_path, self.efuse_full.rx_path,
            "2.4 " if self.efuse_full.has_2ghz else "",
            "5 " if self.efuse_full.has_5ghz else "",
            self.efuse_full.freq_offset, self.efuse_full.temp_offset,
            self.efuse_full.nic_conf_0, self.efuse_full.nic_conf_1,
        )

        # ---- M3c step 17: mt76x02_mac_setaddr.
        # [SRC] mt76x02_mac.c:727-758. Writes MAC + BSSID regs and clears
        # 16 per-vif BSSID slots.
        try:
            mac_setaddr(self.transport, self.efuse_full.mac_bytes)
        except (MACInitError, usb.core.USBError) as e:
            logger.error("MT7610U: mac_setaddr failed: %s", e)
            return False
        return True

    def _connect_init_phy(self):
        """Chunk 5: phy_init + TXOP/US_CYC + mac_start (TRX enable) + power-on calibrate."""
        # ---- M3d: mt76x0_phy_init.
        # [SRC] mt76x0/phy.c:1207-1215. Wraps:
        #   phy_ant_select → phy_rf_init (RF tables + cal) → set_rxpath → set_txdac.
        try:
            phy_init(self.transport, self.mcu, self.efuse_full,
                     is_mt7630=self.is_mt7630)
        except (PHYInitError, usb.core.USBError) as e:
            logger.error("MT7610U: phy_init failed: %s", e)
            return False

        # Readback for assertions. We capture state after the full phy_init.
        from .constants import (
            MT_BBP_AGC,
            MT_BBP_TXBE,
            MT_COEXCFG3 as _MT_COEXCFG3,
            MT_MCU_MEMMAP_RF,
            MT_RF,
            MT_WLAN_FUN_CTRL as _MT_WLAN_FUN_CTRL,
        )
        self.wlan_fun_ctrl_after_ant = self.transport.read32(_MT_WLAN_FUN_CTRL)
        self.coexcfg3_after_ant = self.transport.read32(_MT_COEXCFG3)
        self.bbp_agc0_after_phy = self.transport.read32(MT_BBP_AGC(0))
        self.bbp_txbe5_after_phy = self.transport.read32(MT_BBP_TXBE(5))
        # Read MT_RF(0, 22) via MCU to confirm freq cal write landed.
        try:
            rf22 = self.mcu.random_read(MT_MCU_MEMMAP_RF, [MT_RF(0, 22)])[0]
            self.rf_b0_r22_after_phy = rf22 & 0xFF
        except (MCUError, usb.core.USBError) as e:
            logger.warning("MT7610U: MT_RF(0,22) readback failed (non-fatal): %s", e)

        # ----- M6 (TX-on-air): missing pieces at the bottom of
        # mt76x0u_init_hardware + mt76x0u_start. Without these the chip
        # accepts bulk-OUT but EDCA never grants TXOP, so frames queue
        # in MAC TX FIFO but never modulate onto RF.
        #
        # [SRC] mt76x0/usb.c:171-174 (TXOP_CTRL_CFG + US_CYC_CFG —
        # end of mt76x0u_init_hardware) + mt76x0/usb.c:107-115
        # (mt76x02u_mac_start + phy_calibrate(true) — mt76x0u_start).
        from .constants import (
            MT_TXOP_CTRL_CFG,
            MT_TXOP_EXT_CCA_DLY_DEFAULT,
            MT_TXOP_EXT_CCA_DLY_SHIFT,
            MT_TXOP_TRUN_EN_DEFAULT,
            MT_TXOP_TRUN_EN_SHIFT,
            MT_US_CYC_CFG,
            MT_US_CYC_CNT_DEFAULT,
            MT_US_CYC_CNT_MASK,
        )
        try:
            # MT_US_CYC_CFG: RMW the low 8 bits (CNT field) to 0x1e.
            us_cyc = self.transport.read32(MT_US_CYC_CFG)
            us_cyc = (us_cyc & ~MT_US_CYC_CNT_MASK) | MT_US_CYC_CNT_DEFAULT
            self.transport.write32(MT_US_CYC_CFG, us_cyc)
            # MT_TXOP_CTRL_CFG: full overwrite (kernel uses mt76_wr, not rmw).
            self.transport.write32(
                MT_TXOP_CTRL_CFG,
                (MT_TXOP_TRUN_EN_DEFAULT << MT_TXOP_TRUN_EN_SHIFT)
                | (MT_TXOP_EXT_CCA_DLY_DEFAULT << MT_TXOP_EXT_CCA_DLY_SHIFT),
            )
            logger.info("MT7610U: TXOP_CTRL_CFG + US_CYC_CFG written "
                        "(EDCA TXOP grants now possible)")
        except usb.core.USBError as e:
            logger.error("MT7610U: TXOP/US_CYC write failed: %s", e)
            return False

        # mt76x02u_mac_start: staged ENABLE_TX → wait_for_wpdma → write
        # RX_FILTR_CFG → ENABLE_TX|ENABLE_RX → wait_for_wpdma.
        # MT_MAC_SYS_CTRL_ENABLE_RX / _ENABLE_TX already imported at top.
        try:
            self.transport.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)
            wait_for_wpdma(self.transport, timeout_ms=200)
            # MONITOR-mode RX filter. Take cached default (0x00017f97) and
            # CLEAR bit 2 (MT_RX_FILTR_CFG_PROMISC). The kernel name is
            # misleading: the bit actually means "DROP unicast not
            # addressed to me". STA mode wants it set (kernel's default);
            # monitor mode wants it cleared so we see all unicast traffic
            # — including EAPOL handshakes between every nearby client
            # and its AP. [SRC] mt76x0/main.c:80-86 (IEEE80211_CONF_CHANGE_MONITOR
            # branch) + mt76x2u/mac.py monitor branch.
            #
            # Bit 3 OTHER_BSS is already 0 in the default; explicit-clear
            # for monitor parity. Result: 0x00017f97 → 0x00017f93.
            MT_RX_FILTR_CFG_PROMISC   = 1 << 2
            MT_RX_FILTR_CFG_OTHER_BSS = 1 << 3
            base = (self.rxfilter_default if self.rxfilter_default is not None
                    else 0x00017F97)
            monitor_filter = base & ~(
                MT_RX_FILTR_CFG_PROMISC | MT_RX_FILTR_CFG_OTHER_BSS
            )
            self.transport.write32(MT_RX_FILTR_CFG, monitor_filter)
            logger.info(
                "MT7610U: RX_FILTR_CFG = 0x%08x (monitor — DROP_UC_NOME + "
                "DROP_OTHER_BSS bits cleared; was 0x%08x default)",
                monitor_filter, base,
            )

            # MONITOR-mode address-match override. M3c's mac_setaddr
            # ports kernel verbatim: MT_MAC_ADDR_DW1 has U2ME_MASK=0xff
            # (strict-match unicast first-byte to our MAC) and
            # MT_MAC_BSSID_DW1 has MBSS_MODE=3 + MBSS_LOCAL + MBEACON_N=7
            # (multi-BSSID AP-mode framing). Together these cause the
            # chip's address-match engine to DROP unicast DATA frames
            # not destined for our MAC, even with RX_FILTR_CFG.PROMISC
            # cleared. mt76x2u (working monitor sibling) deliberately
            # writes BARE MAC values to both registers — match that.
            #
            # Symptom this fixes: unicast MGMT (probe-resp/auth/assoc)
            # comes through fine but unicast DATA (incl. EAPOL hand-
            # shakes) is invisible.
            from .constants import MT_MAC_ADDR_DW1, MT_MAC_BSSID_DW1
            if self.efuse_full is not None:
                mac_bytes = self.efuse_full.mac_bytes
                mac_hi = int.from_bytes(mac_bytes[4:6], "little")
                # Plain MAC upper 2 bytes — no U2ME_MASK, no AP-mode bits.
                self.transport.write32(MT_MAC_ADDR_DW1, mac_hi)
                self.transport.write32(MT_MAC_BSSID_DW1, mac_hi)
                logger.info(
                    "MT7610U: MT_MAC_ADDR_DW1=0x%08x, MT_MAC_BSSID_DW1=0x%08x "
                    "(U2ME_MASK + MBSS_MODE/LOCAL/MBEACON_N cleared for monitor)",
                    mac_hi, mac_hi,
                )
            self.transport.write32(
                MT_MAC_SYS_CTRL,
                MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX,
            )
            wait_for_wpdma(self.transport, timeout_ms=50)
        except (usb.core.USBError, MACInitError) as e:
            logger.error("MT7610U: mac_start failed: %s", e)
            return False

        # phy_calibrate(power_on=True) — runs CAL_R + CAL_VCO before the
        # normal CAL_FULL/CAL_LC/CAL_RXDCOC chain. Required for TX RF to
        # actually emit. Kernel calls this from mt76x0u_start after
        # mt76x02u_mac_start. Channel: 6 (we tune properly via set_channel
        # later; this is just the chip's first-time RF cal handshake).
        try:
            from .phy import phy_calibrate
            phy_calibrate(self.transport, self.mcu, channel=6, power_on=True,
                          is_mt7630=self.is_mt7630)
            logger.info("MT7610U: initial phy_calibrate(power_on=True) OK")
        except (MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: initial phy_calibrate failed: %s", e)
            return False
        return True

    def _set_channel_sync(self, channel: int) -> bool:
        """Sync core of `set_channel` — usable from sync contexts (M5 hop
        loop) and from the async Protocol method below.

        Short-circuits if we're already tuned to `channel`. The
        WlanInterface hop loop calls set_channel every `interval` seconds
        even when pinned to a single channel; re-tuning to the same channel
        is ~150 MCU commands of wasted work that blocks the asyncio loop.
        """
        if channel == self.current_channel:
            return True
        # Block until any in-flight (incl. cancelled-but-draining) tune on
        # another executor thread finishes, so the two never interleave their
        # register batches on the USB control endpoint. See _hw_lock in __init__.
        with self._hw_lock:
            WIRE_LOG.marker(f"begin set_channel({channel})")
            try:
                # Kernel mac80211 invokes mt76x0_phy_set_channel TWICE per
                # `iw set channel` (once via .config(CONF_CHANGE_CHANNEL), once
                # via the chandef-update path). Empirical: 2x BW_SETTING, 2x RF
                # writes, 6x cal commands in every kernel set_channel(N) pcap
                # window. Single-shot leaves the chip's MCU mid-state and the
                # next channel switch's first command wedges. See wire-diff
                # against usb_dumps/captures_mt76x0u/capture-2.pcap.
                for _invocation in (1, 2):
                    self.last_set_channel_state = set_channel_20mhz(
                        self.transport, self.mcu, channel,
                        efuse_full=self.efuse_full, is_mt7630=self.is_mt7630,
                    )
                self.current_channel = channel
                logger.info("MT7610U: set_channel(%d) OK", channel)
                WIRE_LOG.marker(f"end set_channel({channel}) OK")
                return True
            except (PHYInitError, MCUError, usb.core.USBError) as e:
                logger.error("MT7610U: set_channel(%d) failed: %s", channel, e)
                WIRE_LOG.marker(f"end set_channel({channel}) FAIL: {e!r}")
                return False

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Runs the full `mt76x0_phy_set_channel` chain for 20 MHz monitor mode.

        The body is synchronous (~150 MCU commands via PyUSB), so we offload
        to the default executor — mirrors `chips/rtl8188eus/driver.py`. Calling
        the sync helper directly here would block the asyncio event loop for
        the full set_channel duration and freeze the UI + the RX drainer.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._set_channel_sync, channel)

    def enable_trx(self) -> None:
        """M4b — enable MAC TX+RX engines. [SRC] mt76x02_mac.c:1071-1072.

        The kernel always writes ENABLE_TX | ENABLE_RX together (the chip
        misbehaves if only one is set). RX filter stays at the cached
        default (0x00017f97 — drop CRC/PHY/VER errors, accept everything
        else; OTHER_BSS bit is already 0 in the default, so we'll see
        frames from any BSS).
        """
        self.transport.write32(
            MT_MAC_SYS_CTRL,
            MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX,
        )
        logger.info("MT7610U: MAC TRX enabled "
                    "(MAC_SYS_CTRL = ENABLE_TX | ENABLE_RX = 0x0C)")

    def _drain_bulk_in_to_empty(self, max_iters: int = 32,
                                bufsize: int = 2048) -> int:
        """Drain EP 0x84 with a tight 20ms timeout until empty. Returns the
        number of bytes drained. Used between channel changes to keep the
        chip's RX-DMA from backing up while we're issuing MCU commands."""
        bytes_drained = 0
        for _ in range(max_iters):
            try:
                chunk = self.transport.bulk_in(
                    EP_IN_PKT_RX, bufsize, timeout_ms=20,
                )
            except usb.core.USBError as e:
                if (getattr(e, "backend_error_code", None) == -7
                        or getattr(e, "errno", None) == 110):
                    break
                logger.warning("_drain_bulk_in_to_empty: USBError: %s", e)
                break
            if not chunk:
                break
            bytes_drained += len(chunk)
        return bytes_drained

    def scan_channels(
        self, channels: list[int], dwell_ms: int = 400, bufsize: int = 2048,
    ) -> dict:
        """M5 — synchronously hop through `channels`, drain + parse on each
        for `dwell_ms` ms, return per-channel + per-BSSID summary.

        Returns a dict with `per_channel` + `bssids` (per-BSSID aggregate
        across all channels). Caller decides whether to print BSSIDs/SSIDs
        ([[no-ssids-in-commits]] applies to GIT artifacts, not interactive
        test output).

        Robustness:
          - Disables RX (clears MAC_SYS_CTRL ENABLE_RX) before each
            `set_channel`, re-enables after. Necessary because the chip
            wedges if MCU commands run while RX-DMA is backed up — observed
            on hops past ch 6 with TRX always-on.
          - Drains EP 0x84 to empty before set_channel as belt-and-suspenders.
        """
        import time as _time

        from wifit3.wlan.packet import WlanFrameParser, BeaconPacket

        from .constants import (
            MT_MAC_SYS_CTRL,
            MT_MAC_SYS_CTRL_ENABLE_RX,
            MT_MAC_SYS_CTRL_ENABLE_TX,
        )
        from .rx import decode_rx_packet

        # Per-BSSID aggregate across all channels.
        # bssid -> {ssid, channel_seen, encryption, beacons, rssi_dbm_max, last_ch}
        bssids_seen: dict[str, dict] = {}

        per_channel: dict[int, dict] = {}
        total_beacons = 0
        dwell_seconds = dwell_ms / 1000.0

        # Start with TRX disabled. We'll toggle around set_channel.
        self.transport.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)

        for ch in channels:
            # Pause RX before channel change. TX stays on (kernel pattern).
            self.transport.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)
            # Drain anything the chip already pushed before we paused.
            self._drain_bulk_in_to_empty()

            t0 = _time.monotonic()
            try:
                ok2 = self._set_channel_sync(ch)
            except Exception as e:
                logger.warning("scan_channels: ch %d set_channel exception: %s", ch, e)
                ok2 = False
            tune_ms = (_time.monotonic() - t0) * 1000.0

            if not ok2:
                per_channel[ch] = {
                    "beacons": 0, "bssids": 0, "bytes": 0,
                    "rssi_dbm_max": None, "tune_ms": tune_ms,
                    "set_channel_failed": True,
                }
                continue

            # Re-enable RX for the dwell.
            self.transport.write32(
                MT_MAC_SYS_CTRL,
                MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX,
            )

            ch_bssids: set[str] = set()
            ch_beacons = 0
            ch_bytes = 0
            ch_rssi_max: Optional[int] = None
            deadline = _time.monotonic() + dwell_seconds
            while _time.monotonic() < deadline:
                try:
                    chunk = self.transport.bulk_in(
                        EP_IN_PKT_RX, bufsize, timeout_ms=100,
                    )
                except usb.core.USBError as e:
                    if (getattr(e, "backend_error_code", None) == -7
                            or getattr(e, "errno", None) == 110):
                        continue
                    logger.warning("scan_channels: ch %d USBError: %s", ch, e)
                    continue
                if not chunk:
                    continue
                ch_bytes += len(chunk)
                rx = decode_rx_packet(bytes(chunk))
                if rx is None:
                    continue
                if ch_rssi_max is None or rx.rssi_dbm > ch_rssi_max:
                    ch_rssi_max = rx.rssi_dbm
                parsed = WlanFrameParser.parse_80211_frame(rx.frame, rx.rssi_dbm)
                if parsed is None:
                    continue
                if isinstance(parsed, BeaconPacket) and parsed.type == "beacon":
                    ch_beacons += 1
                    bssid = parsed.bssid
                    if not bssid:
                        continue
                    ch_bssids.add(bssid)
                    entry = bssids_seen.setdefault(bssid, {
                        "ssid": parsed.ssid,
                        "encryption": parsed.encryption,
                        "channel_seen_on": ch,
                        "beacons": 0,
                        "rssi_dbm_max": rx.rssi_dbm,
                        "channels": set(),
                    })
                    entry["beacons"] += 1
                    if rx.rssi_dbm > entry["rssi_dbm_max"]:
                        entry["rssi_dbm_max"] = rx.rssi_dbm
                    entry["channels"].add(ch)
                    # Track the first non-empty SSID we get.
                    if not entry["ssid"] and parsed.ssid:
                        entry["ssid"] = parsed.ssid

            per_channel[ch] = {
                "beacons":      ch_beacons,
                "bssids":       len(ch_bssids),
                "bytes":        ch_bytes,
                "rssi_dbm_max": ch_rssi_max,
                "tune_ms":      tune_ms,
            }
            total_beacons += ch_beacons

        # Park with RX disabled (caller can re-enable if it wants more).
        self.transport.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)

        return {
            "per_channel":     per_channel,
            "bssids":          bssids_seen,
            "total_bssids":    len(bssids_seen),
            "total_beacons":   total_beacons,
            "channels_dwelt":  len(channels),
        }

    def drain_bulk_in_parsed(
        self, duration_seconds: float, bufsize: int = 2048,
        timeout_ms: int = 200,
    ) -> dict:
        """M4c — drain EP 0x84, decode each packet via `rx.decode_rx_packet`,
        feed the 802.11 frame to `WlanFrameParser`. Returns aggregated stats
        WITHOUT leaking SSIDs/BSSIDs (per [[no-ssids-in-commits]]):
          - total bytes / xfers / timeouts / errors / decode_failures
          - count by frame type/subtype (beacon / probe_req / probe_resp /
            data / other_mgmt / ctrl)
          - unique BSSID count (set size, not the values)
          - parsed RSSI min/max/mean
        """
        import time as _time

        from wifit3.wlan.packet import WlanFrameParser

        from .rx import decode_rx_packet

        counters = {
            "bytes": 0, "xfers": 0, "timeouts": 0, "errors": 0,
            "decoded": 0, "decode_failures": 0,
            "beacon": 0, "probe_req": 0, "probe_resp": 0,
            "deauth_disassoc": 0,
            "other_mgmt": 0, "data": 0, "ctrl": 0,
            "parse_failures": 0,
        }
        bssids: set[str] = set()
        rssi_values: list[int] = []

        deadline = _time.monotonic() + duration_seconds
        while _time.monotonic() < deadline:
            try:
                chunk = self.transport.bulk_in(
                    EP_IN_PKT_RX, bufsize, timeout_ms=timeout_ms,
                )
            except usb.core.USBError as e:
                if (getattr(e, "backend_error_code", None) == -7
                        or getattr(e, "errno", None) == 110):
                    counters["timeouts"] += 1
                    continue
                counters["errors"] += 1
                logger.warning("drain_bulk_in_parsed: USBError: %s", e)
                continue

            if not chunk:
                continue
            counters["bytes"] += len(chunk)
            counters["xfers"] += 1

            rx = decode_rx_packet(bytes(chunk))
            if rx is None:
                counters["decode_failures"] += 1
                continue
            counters["decoded"] += 1

            parsed = WlanFrameParser.parse_80211_frame(rx.frame, rx.rssi_dbm)
            if parsed is None:
                counters["parse_failures"] += 1
                continue

            ftype = parsed.type_id
            subtype = parsed.subtype_id
            if ftype == WlanFrameParser.TYPE_MGMT:
                if subtype == WlanFrameParser.SUBTYPE_BEACON:
                    counters["beacon"] += 1
                elif subtype == WlanFrameParser.SUBTYPE_PROBE_REQ:
                    counters["probe_req"] += 1
                elif subtype == WlanFrameParser.SUBTYPE_PROBE_RESP:
                    counters["probe_resp"] += 1
                elif subtype in (0x0A, 0x0C):    # disassoc, deauth
                    counters["deauth_disassoc"] += 1
                else:
                    counters["other_mgmt"] += 1
            elif ftype == WlanFrameParser.TYPE_DATA:
                counters["data"] += 1
            elif ftype == WlanFrameParser.TYPE_CTRL:
                counters["ctrl"] += 1

            bssid = parsed.bssid
            if bssid:
                bssids.add(bssid)

            rssi_values.append(rx.rssi_dbm)

        if rssi_values:
            counters["rssi_min"] = min(rssi_values)
            counters["rssi_max"] = max(rssi_values)
            counters["rssi_mean"] = sum(rssi_values) // len(rssi_values)
        counters["unique_bssids"] = len(bssids)
        return counters

    def drain_bulk_in(
        self, duration_seconds: float, bufsize: int = 2048,
        timeout_ms: int = 200,
    ) -> dict:
        """M4b — drain EP 0x84 bulk-IN for N seconds. Returns stats dict
        with `bytes`, `xfers`, `timeouts`, `errors`.

        Each bulk-IN response is a `[mt76 RX desc][802.11 frame][padding]`
        blob; we don't decode it here (M4c will). Goal is to confirm raw
        bytes are flowing — i.e., the chip is actually receiving on the
        configured channel.
        """
        import time as _time
        stats = {"bytes": 0, "xfers": 0, "timeouts": 0, "errors": 0,
                 "first_chunk": None}
        deadline = _time.monotonic() + duration_seconds
        while _time.monotonic() < deadline:
            try:
                data = self.transport.bulk_in(
                    EP_IN_PKT_RX, bufsize, timeout_ms=timeout_ms,
                )
                if data:
                    stats["bytes"] += len(data)
                    stats["xfers"] += 1
                    if stats["first_chunk"] is None:
                        stats["first_chunk"] = bytes(data[:32])
            except usb.core.USBError as e:
                # libusb backend ETIMEDOUT (-7) or PyUSB errno.ETIMEDOUT.
                if (getattr(e, "backend_error_code", None) == -7
                        or getattr(e, "errno", None) == 110):
                    stats["timeouts"] += 1
                    continue
                stats["errors"] += 1
                logger.warning("drain_bulk_in: USBError: %s", e)
        return stats

    def _set_ack_admit(self, admit: bool) -> int:
        """RMW MT_RX_FILTR_CFG bit 10 (MT_RX_FILTR_CFG_ACK): clear to admit the AP's
        link-layer ACKs into monitor RX, set to restore the drop. Under _hw_lock so it
        can't interleave a set_channel register batch. Returns the new value."""
        with self._hw_lock:
            cur = self.transport.read32(MT_RX_FILTR_CFG)
            new = cur & ~MT_RX_FILTR_CFG_ACK if admit else cur | MT_RX_FILTR_CFG_ACK
            if new != cur:
                self.transport.write32(MT_RX_FILTR_CFG, new)
            return new

    async def enable_ack_detect(self) -> None:
        """Admit ACK control frames (clear RX_FILTR_CFG_ACK) so the tap sees the AP's
        ACKs to our injected MAC. Not active monitor, which makes the chip emit ACKs."""
        loop = asyncio.get_running_loop()
        new = await loop.run_in_executor(None, self._set_ack_admit, True)
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._all_acks_seen = 0
        self._tx_frames = 0
        self._tx_unacked = 0
        self._ack_detect_on = True
        logger.info("MT7610U TX-ACK detection ON (RX_FILTR_CFG=0x%08x, ACK bit clear) "
                    "— observing our TX delivery", new)

    async def disable_ack_detect(self) -> None:
        """Restore the default monitor RX filter (re-set RX_FILTR_CFG_ACK)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_ack_admit, False)
        self._ack_detect_on = False

    def acks_seen(self, mac: bytes) -> int:
        """Count of ACKs observed addressed to ``mac`` (an injected source MAC) since enable."""
        return self._ack_sightings.get(bytes(mac).hex(), 0)

    async def _await_ack(self, ta: bytes, since: float, window: float) -> bool:
        """True if the tap observed an ACK to ``ta`` after ``since``, within ``window`` s.
        _on_raw_rx runs on this loop, so a sleep yield lets a just-arrived ACK's timestamp
        land between checks."""
        deadline = since + window
        while time.monotonic() < deadline:
            if self._ack_last_ts.get(ta, 0.0) > since:
                return True
            await asyncio.sleep(0.001)
        return False

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """M6a — inject a raw 802.11 frame via EP 0x09 (MGMT queue).

        `use_no_ack=True` (Protocol default) skips ACK_REQ — appropriate
        for spoofed-source frames (deauths/disassocs) where the target
        wouldn't ACK to a random MAC anyway. Pass False for unicast frames
        from a real associated station MAC.

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and
        resends the identical frame up to ``max_resends`` times if none comes, returning
        whether it landed; ``0`` = fire-and-forget (current behaviour).
        """
        from .tx import TXError, inject_80211_frame
        ta = bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None   # TA — the AP ACKs back to this
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None

        def _send() -> bool:
            try:
                n = inject_80211_frame(
                    self.transport, frame_bytes,
                    request_ack=not use_no_ack, wcid=0xFF,
                )
                logger.debug("MT7610U: inject_frame(%d bytes) → %d bulk-OUT bytes",
                             len(frame_bytes), n)
                return True
            except TXError as e:
                logger.error("MT7610U: inject_frame failed: %s", e)
                return False
            except usb.core.USBError as e:
                logger.error("MT7610U: inject_frame USB error: %s", e)
                return False

        if not ack_gated:
            return _send()                  # fire-and-forget (deauth / WEP / current behaviour)
        for _ in range(max_resends + 1):
            t0 = time.monotonic()
            ok = _send()
            self._tx_frames += 1
            if ok and await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        self._tx_unacked += 1
        return False                        # never ACKed after every send

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Set MT_MAC_ADDR_DW0/1 to ``mac`` with U2ME_MASK=0xff so the autoresponder
        HW-ACKs frames to it (the monitor baseline runs U2ME=0). Reversed by exit."""
        await self._write_self_mac(bytes(mac), u2me=True)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the monitor baseline: real MAC with U2ME_MASK cleared."""
        if not self.mac_address:
            return
        real = bytes(int(b, 16) for b in self.mac_address.split(":"))
        await self._write_self_mac(real, u2me=False)

    async def _write_self_mac(self, mac: bytes, u2me: bool) -> None:
        from .constants import MT_MAC_ADDR_DW0, MT_MAC_ADDR_DW1, MT_MAC_ADDR_DW1_U2ME_MASK
        lo = int.from_bytes(mac[0:4], "little")
        hi = int.from_bytes(mac[4:6], "little") | (MT_MAC_ADDR_DW1_U2ME_MASK if u2me else 0)

        def _write():
            with self._hw_lock:
                self.transport.write32(MT_MAC_ADDR_DW0, lo)
                self.transport.write32(MT_MAC_ADDR_DW1, hi)
        await asyncio.get_running_loop().run_in_executor(None, _write)

    async def close(self) -> None:
        WIRE_LOG.marker("close")
        if self._rx_drainer is not None:
            try:
                await self._rx_drainer.stop()
            except Exception as e:
                logger.debug("MT7610U: rx_drainer.stop ignored: %s", e)
            self._rx_drainer = None
        self.transport.dispose()
        WIRE_LOG.close()
