"""MT76x0U / MT7610U driver — WlanDriver Protocol implementation (M1).

Ported from Linux mt76 (kernel v6.18) for wifit3, 2026.

M1 scope: claim USB interface, upload mt7610e.bin firmware, ack FW_READY.
PHY init / RX / TX land in M2..M4.

Per [[feedback_prefer_fork_over_base]] this is a fresh sibling of
chips/mt76x2u/driver.py — same family, no shared imports.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback

from .constants import (
    EP_IN_PKT_RX,
    MT_MAC_STATUS,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_RX,
    MT_MAC_SYS_CTRL_ENABLE_TX,
    MT_RX_FILTR_CFG,
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
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)

# FW lives next to this module in assets/.
ASSETS_DIR = Path(__file__).parent / "assets"
FW_FILE_PRIMARY = ASSETS_DIR / "mt7610e_linux-firmware.bin"
FW_FILE_FALLBACK = ASSETS_DIR / "mt7610u_linux-firmware.bin"


class MT76x0UDriver:
    """Driver for MT7610U-family USB cards (Alfa AWUS036ACM mt7610u variant,
    Sabrent NTWLAC, ...). WIRE-verified on 0e8d:7610.

    M1 only does enough to upload firmware and confirm FW_READY.
    """

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

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device,
                        id_entry: DeviceID) -> "MT76x0UDriver":
        return cls(dev, id_entry)

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID):
        self.dev = dev
        self.id_entry = id_entry
        self.transport = MT76x0UTransport(dev)
        self.mcu = MCUChannel(self.transport)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
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

    # ---- Hooks --------------------------------------------------------
    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    # ---- Lifecycle ----------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M1: USB reset + claim interface + FW upload to FW_READY ack."""
        # ---- usb_reset_device equivalent. [SRC] mt76x0/usb.c:249
        # The kernel probe path does this BEFORE any chip access. On Linux
        # the implicit usb open often triggers a similar reset; on Windows +
        # WinUSB it doesn't, so we must call it explicitly. Without this the
        # first vendor write after mt76x02u_mcu_fw_reset stalls — the chip's
        # MCU enters a state it never properly recovers from.
        if progress_cb:
            progress_cb(0.005, "USB port reset (usb_reset_device equivalent)")
        try:
            self.dev.reset()
            logger.info("MT7610U: dev.reset() OK")
        except usb.core.USBError as e:
            # On some platforms / kernels the reset returns ENODEV briefly as
            # the device re-enumerates. Log and continue — if the chip is
            # truly gone, the next claim will fail loudly.
            logger.warning("MT7610U: dev.reset() raised %s (continuing)", e)

        if progress_cb:
            progress_cb(0.01, "Claiming MT7610U interface")
        try:
            self.transport.claim()
        except RuntimeError as e:
            logger.error("MT7610U: %s", e)
            return False

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
                return False

        if progress_cb:
            progress_cb(0.05, f"Uploading firmware ({fw_file.name})")

        uploader = FirmwareUploader(
            self.transport,
            progress_cb=(
                lambda pct, msg: progress_cb(0.05 + pct * 0.90, msg)
                if progress_cb else None
            ),
        )
        try:
            result = uploader.load_firmware(fw_file)
        except FirmwareError as e:
            logger.error("MT7610U FW upload failed: %s", e)
            return False
        except usb.core.USBError as e:
            logger.error("MT7610U: USB error during FW upload: %s", e)
            return False

        self.fw_info = result
        self.is_warm = result["skipped"]
        if result["skipped"]:
            logger.info("MT7610U: warm boot — FW already running")
        else:
            h = result["header"]
            logger.info(
                "MT7610U: FW v%s build 0x%04x (%s) — ready after %d poll(s)",
                h["fw_ver_str"], h["build_ver"], h["build_time"], result["polls"],
            )

        # Post-FW driver flow mirrors mt76x0u_init_hardware (mt76x0/usb.c:151)
        # which after mcu_init does:
        #   init_usb_dma → wait_for_wpdma → wait_for_mac → reset_csr_bbp →
        #   Q_SELECT → init_mac_registers → wait_for_txrx_idle → init_bbp → ...
        # M1 covered through mcu_init. M2 added init_usb_dma + reset_csr_bbp +
        # Q_SELECT (out of strict kernel order; works because USB chip has no
        # WPDMA busy state to wait on). M3a inserts the missing waits + the
        # MAC reg init + wait_for_txrx_idle.

        # ---- Post-FW step 6: init_usb_dma (kernel mt76x0_init_usb_dma).
        if progress_cb:
            progress_cb(0.91, "init_usb_dma (RX_DROP_OR_PAD toggle)")
        try:
            uploader.init_usb_dma()
        except usb.core.USBError as e:
            logger.error("MT7610U: init_usb_dma failed: %s", e)
            return False

        # ---- M3a step 7: wait_for_wpdma. [SRC] mt76x02_dma.h:54-60,
        # [SRC] mt76x0/init.c:175. Returns immediately on USB (no WPDMA busy).
        if progress_cb:
            progress_cb(0.92, "wait_for_wpdma")
        if not wait_for_wpdma(self.transport):
            logger.error("MT7610U: wait_for_wpdma timed out")
            return False

        # ---- M3a step 8: wait_for_mac (second time, post-FW upload).
        # Kernel does this in init_hardware:179. Already done once during M1
        # (after chip_onoff), but the kernel re-checks here too — port faithfully.
        if progress_cb:
            progress_cb(0.93, "wait_for_mac (post-FW)")
        try:
            uploader.wait_for_mac()
        except FirmwareError as e:
            logger.error("MT7610U: wait_for_mac (post-FW) failed: %s", e)
            return False

        # ---- Post-FW step 9: reset_csr_bbp [SRC] mt76x0/init.c:182.
        if progress_cb:
            progress_cb(0.94, "reset_csr_bbp (200ms MAC reset)")
        try:
            uploader.reset_csr_bbp()
        except usb.core.USBError as e:
            logger.error("MT7610U: reset_csr_bbp failed: %s", e)
            return False

        # ---- Post-FW step 10: Q_SELECT [SRC] mt76x0/init.c:183 —
        # `mt76x02_mcu_function_select(dev, Q_SELECT, 1)`.
        # [WIRE] capture-2.pcap:423 payload `01000000 01000000`.
        if progress_cb:
            progress_cb(0.95, "MCU Q_SELECT (CMD_FUN_SET_OP, no-wait)")
        try:
            from .constants import Q_SELECT
            self.mcu.function_select(Q_SELECT, 1)
        except (MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: MCU Q_SELECT failed: %s", e)
            return False

        # ---- M2 diagnostic: MCU smoke-test ----------------------------
        # Verifies the MCU command channel before we drive the init tables
        # through it. Kept from M2 — not strictly in kernel flow but cheap.
        if progress_cb:
            progress_cb(0.96, "MCU CMD_RANDOM_READ smoke test")
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
        except (MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: MCU smoke test failed: %s", e)
            return False

        # ---- M3a step 11: init_mac_registers [SRC] mt76x0/init.c:187.
        # Uploads common_mac_reg_table + mt76x0_mac_reg_table via MCU, then
        # 4 direct register tweaks (release MAC reset, EXT_CCA_CFG, FCE_L2_STUFF,
        # WMM_CTRL).
        if progress_cb:
            progress_cb(0.96, "init_mac_registers (66 table + 4 direct writes)")
        try:
            init_mac_registers(self.transport, self.mcu)
        except (MCUError, MACInitError, usb.core.USBError) as e:
            logger.error("MT7610U: init_mac_registers failed: %s", e)
            return False

        # ---- M3a step 12: wait_for_txrx_idle [SRC] mt76x0/init.c:189.
        if progress_cb:
            progress_cb(0.96, "wait_for_txrx_idle (MAC_STATUS TX|RX=0)")
        if not wait_for_txrx_idle(self.transport):
            logger.error("MT7610U: wait_for_txrx_idle timed out")
            return False
        self.mac_status_after_init = self.transport.read32(MT_MAC_STATUS)
        logger.info("MT7610U: MAC_STATUS after init = 0x%08x (TX|RX idle)",
                    self.mac_status_after_init)

        # ---- M3b: init_bbp [SRC] mt76x0/init.c:192.
        # phy_wait_bbp_ready, then bbp_init_tab (58 pairs MCU), then 20
        # filtered switch_tab entries direct-write, then dcoc_tab (9 pairs MCU).
        if progress_cb:
            progress_cb(0.85, "init_bbp (BBP wait + 3 tables)")
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

        # ---- M3c step 14: clear all 16x4 shared keys.
        # [SRC] mt76x0/init.c:198-200.
        if progress_cb:
            progress_cb(0.88, "clear_shared_keys (16 vifs × 4 keys)")
        try:
            clear_shared_keys(self.transport)
        except usb.core.USBError as e:
            logger.error("MT7610U: clear_shared_keys failed: %s", e)
            return False

        # ---- M3c step 15: clear all 256 WCIDs.
        # [SRC] mt76x0/init.c:202-203.
        if progress_cb:
            progress_cb(0.92, "clear_wcids (256 entries)")
        try:
            clear_wcids(self.transport)
        except usb.core.USBError as e:
            logger.error("MT7610U: clear_wcids failed: %s", e)
            return False

        # ---- M3c step 16: full eeprom_init.
        # [SRC] mt76x0/init.c:205 + mt76x0/eeprom.c:312-353.
        if progress_cb:
            progress_cb(0.96, "eeprom_init (full 512-byte EFUSE)")
        try:
            self.efuse_full = read_efuse_full(self.transport)
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
        if progress_cb:
            progress_cb(0.96, "mac_setaddr (MAC + BSSID regs + 16 slot clear)")
        try:
            mac_setaddr(self.transport, self.efuse_full.mac_bytes)
        except (MACInitError, usb.core.USBError) as e:
            logger.error("MT7610U: mac_setaddr failed: %s", e)
            return False

        # ---- M3d: mt76x0_phy_init.
        # [SRC] mt76x0/phy.c:1207-1215. Wraps:
        #   phy_ant_select → phy_rf_init (RF tables + cal) → set_rxpath → set_txdac.
        if progress_cb:
            progress_cb(0.97, "phy_init (ant_select + rf_init + rxpath + txdac)")
        try:
            phy_init(self.transport, self.mcu, self.efuse_full)
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

        if progress_cb:
            progress_cb(1.00, "M3d complete — phy_init done")
        return True

    def _set_channel_sync(self, channel: int) -> bool:
        """Sync core of `set_channel` — usable from sync contexts (M5 hop
        loop) and from the async Protocol method below."""
        try:
            self.last_set_channel_state = set_channel_20mhz(
                self.transport, self.mcu, channel,
                efuse_full=self.efuse_full,
            )
            self.current_channel = channel
            logger.info("MT7610U: set_channel(%d) OK", channel)
            return True
        except (PHYInitError, MCUError, usb.core.USBError) as e:
            logger.error("MT7610U: set_channel(%d) failed: %s", channel, e)
            return False

    async def set_channel(self, channel: int) -> bool:
        """Runs the full `mt76x0_phy_set_channel` chain (M4a) for 20 MHz
        monitor mode. Body is synchronous; the `async def` exists for the
        WlanDriver Protocol contract.
        """
        return self._set_channel_sync(channel)

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

        from wifit3.wlan.packet import WlanFrameParser

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
                if (parsed.get("type_id") == WlanFrameParser.TYPE_MGMT
                        and parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON):
                    ch_beacons += 1
                    bssid = parsed.get("bssid")
                    if not bssid:
                        continue
                    ch_bssids.add(bssid)
                    entry = bssids_seen.setdefault(bssid, {
                        "ssid": parsed.get("ssid"),
                        "encryption": parsed.get("encryption", "?"),
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
                    if not entry["ssid"] and parsed.get("ssid"):
                        entry["ssid"] = parsed.get("ssid")

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

            ftype = parsed.get("type_id")
            subtype = parsed.get("subtype_id")
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

            bssid = parsed.get("bssid")
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

    async def inject_frame(self, frame_bytes: bytes,
                           use_no_ack: bool = True) -> bool:
        """Not implemented in M1 — lands in M4."""
        raise NotImplementedError("MT7610U inject_frame is an M4 milestone")

    async def close(self) -> None:
        self.transport.dispose()
