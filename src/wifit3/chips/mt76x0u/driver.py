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

from .constants import USB_IDS_MT76X0U
from .firmware import FirmwareError, FirmwareUploader
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
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._rx_callback: Optional[Callable[[dict], None]] = None
        # M1 result, populated by connect().
        self.fw_info: Optional[dict] = None

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
        if progress_cb:
            progress_cb(1.00, "M1 complete — FW_READY")
        return True

    async def set_channel(self, channel: int) -> bool:
        """Not implemented in M1 — lands in M3."""
        raise NotImplementedError("MT7610U set_channel is an M3 milestone")

    async def inject_frame(self, frame_bytes: bytes,
                           use_no_ack: bool = True) -> bool:
        """Not implemented in M1 — lands in M4."""
        raise NotImplementedError("MT7610U inject_frame is an M4 milestone")

    async def close(self) -> None:
        self.transport.dispose()
