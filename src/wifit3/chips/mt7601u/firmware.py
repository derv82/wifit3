"""Firmware image validation and the register preamble the download needs.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/mcu.c:411
(mt7601u_load_firmware) and the mt76_fw_header struct at mcu.c:268.

The image is the standard mt7601u.bin: a 32-byte header, then a 64-byte IVB, then
the ILM image, then the DLM image. We replay the vendor download path -- register
and RAM writes only. No fuse is programmed, and nothing here is card-specific.
"""
from __future__ import annotations

import logging
import lzma
import subprocess
import time
from pathlib import Path

from .constants import (
    MT_FCE_PDMA_GLOBAL_CONF,
    MT_FCE_PSE_CTRL,
    MT_FCE_SKIP_FS,
    MT_PBF_CFG,
    MT_PBF_CFG_TX0Q_EN,
    MT_PBF_CFG_TX1Q_EN,
    MT_PBF_CFG_TX2Q_EN,
    MT_PBF_CFG_TX3Q_EN,
    Q_SELECT,
    MT_TX_CPU_FROM_FCE_BASE_PTR,
    MT_TX_CPU_FROM_FCE_MAX_COUNT,
    MT_USB_DMA_CFG,
    MT_USB_DMA_CFG_RX_BULK_EN,
    MT_USB_DMA_CFG_TX_BULK_EN,
    MT_USB_DMA_CFG_TX_CLR,
)
from .mcu import MT7601UMcu
from .transport import MT7601UTransport

logger = logging.getLogger(__name__)

FW_HEADER_LEN = 32
"""sizeof(struct mt76_fw_header) -- 4+4+2+2+4+16."""
MAX_FIRMWARE_BYTES = 1 << 22
"""Sanity bound on a decompressed image; the real one is ~45 KB."""

FCE_TX_FS_BASE_PTR = 0x400230
"""mcu.c:484 -- the FCE tx_fs_base_ptr the vendor code sets."""
FCE_PDMA_GLOBAL_CONF_VALUE = 0x44
"""mcu.c:488 -- FCE pdma enable bits."""
FCE_SKIP_FS_VALUE = 3
"""mcu.c:490 -- skip_fs_en."""

# Bare register offsets the kernel writes with no symbolic name (mcu.c:459-468).
_REG_94C = 0x94C
_REG_A44 = 0xA44
_REG_230 = 0x230
_REG_400 = 0x400
_REG_800 = 0x800

_VALUE_84210 = 0x84210
_VALUE_80C00 = 0x80C00
_VEND_RESET_DELAY_S = 0.005


class FirmwareError(Exception):
    """The image is missing, truncated, or has an impossible header."""


def find_firmware(explicit: str | Path | None = None) -> Path:
    """Locate mt7601u.bin, trying the kernel's paths in order (mcu.c:406).

    Distros ship it zstd-compressed (`.bin.zst`); the kernel's firmware loader
    decompresses transparently, so we do too.
    """
    if explicit:
        candidates = [Path(explicit)]
    else:
        candidates = []
        for stem in ("mediatek/mt7601u.bin", "mt7601u.bin"):
            base = Path("/usr/lib/firmware") / stem
            candidates += [base, base.with_suffix(base.suffix + ".zst"),
                           base.with_suffix(base.suffix + ".xz")]
        candidates.append(Path(__file__).parent / "assets" / "mt7601u.bin")
    for path in candidates:
        if path.is_file():
            return path
    raise FirmwareError("mt7601u.bin not found in any of: "
                        + ", ".join(str(c) for c in candidates))


def _decompress_zstd(path: Path) -> bytes:
    """Decompress a .zst blob, preferring the zstandard module over the CLI.

    Not taking a new dependency for one firmware blob: the CLI ships with zstd,
    which any distribution already carrying a .zst firmware has.
    """
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(
            path.read_bytes(), max_output_size=MAX_FIRMWARE_BYTES)
    except ImportError:
        pass
    try:
        proc = subprocess.run(["zstd", "-d", "-c", str(path)],
                              capture_output=True, timeout=30)
    except FileNotFoundError as e:
        raise FirmwareError(f"{path.name} is zstd-compressed but neither the "
                            f"zstandard module nor the zstd CLI is available") from e
    if proc.returncode != 0:
        raise FirmwareError(f"zstd failed on {path.name}: "
                            f"{proc.stderr.decode('ascii', 'replace').strip()}")
    return proc.stdout


def read_firmware(path: Path) -> bytes:
    """Read a firmware blob, decompressing by extension."""
    if path.suffix == ".zst":
        return _decompress_zstd(path)
    raw = path.read_bytes()
    if path.suffix == ".xz":
        return lzma.decompress(raw)
    return raw


def validate(fw: bytes) -> None:
    """Check the header is self-consistent (mcu.c:436-449)."""
    if len(fw) < FW_HEADER_LEN:
        raise FirmwareError("firmware shorter than its header")
    ilm_len = int.from_bytes(fw[0:4], "little")
    dlm_len = int.from_bytes(fw[4:8], "little")
    if ilm_len <= 64:                        # MT_MCU_IVB_SIZE, mcu.c:441
        raise FirmwareError(f"implausible ILM length {ilm_len}")
    expected = FW_HEADER_LEN + ilm_len + dlm_len
    if len(fw) != expected:
        raise FirmwareError(f"firmware size {len(fw)} != header-implied {expected}")


def describe(fw: bytes) -> str:
    """The version string the kernel logs (mcu.c:451)."""
    fw_ver = int.from_bytes(fw[10:12], "little")
    build_ver = int.from_bytes(fw[8:10], "little")
    build_time = fw[16:32].split(b"\x00")[0].decode("ascii", "replace")
    return (f"Firmware Version: {(fw_ver >> 12) & 0xF}.{(fw_ver >> 8) & 0xF}."
            f"{fw_ver & 0xF:02d} Build: {build_ver:x} Build time: {build_time}")


def load_firmware(tp: MT7601UTransport, mcu: MT7601UMcu,
                  image_path: str | Path | None = None) -> bool:
    """Run mt7601u_load_firmware: preamble, then the image download.

    The preamble is not optional -- it configures the FCE DMA engine and the USB
    bulk pipes the firmware image itself is transferred through. Returns True when
    mcu.c:416 firmware_running took the warm shortcut and nothing was downloaded.
    """
    fw = read_firmware(find_firmware(image_path))
    validate(fw)
    logger.info(describe(fw))

    # Enable the bulk pipes; without this the download has no path to the chip.
    tp.wr(MT_USB_DMA_CFG, MT_USB_DMA_CFG_RX_BULK_EN | MT_USB_DMA_CFG_TX_BULK_EN)

    if mcu.firmware_running():
        # Already up from a previous session: nothing to download.
        mcu.mcu_running = True
        logger.info("Firmware already running, skipping download")
        return True

    tp.wr(_REG_94C, 0)
    tp.wr(MT_FCE_PSE_CTRL, 0)

    tp.reset()                                   # mcu.c:462 vendor_reset
    time.sleep(_VEND_RESET_DELAY_S)

    tp.wr(_REG_A44, 0)
    tp.wr(_REG_230, _VALUE_84210)
    tp.wr(_REG_400, _VALUE_80C00)
    tp.wr(_REG_800, 1)

    # Open all four TX queue gates. mask=0 means "OR everything back in".
    tp.rmw(MT_PBF_CFG, 0,
           MT_PBF_CFG_TX0Q_EN | MT_PBF_CFG_TX1Q_EN | MT_PBF_CFG_TX2Q_EN | MT_PBF_CFG_TX3Q_EN)

    tp.wr(MT_FCE_PSE_CTRL, 1)

    tp.wr(MT_USB_DMA_CFG, MT_USB_DMA_CFG_RX_BULK_EN | MT_USB_DMA_CFG_TX_BULK_EN)
    # mt76_set is mt76_rmw(reg, 0, bit) (mt7601u.h:316): it READS, SETS the bit and
    # WRITES, then the next line clears it again. Both writes reach the chip.
    val = tp.rmw(MT_USB_DMA_CFG, 0, MT_USB_DMA_CFG_TX_CLR)
    val &= ~MT_USB_DMA_CFG_TX_CLR & 0xFFFFFFFF
    tp.wr(MT_USB_DMA_CFG, val)

    tp.wr(MT_TX_CPU_FROM_FCE_BASE_PTR, FCE_TX_FS_BASE_PTR)
    tp.wr(MT_TX_CPU_FROM_FCE_MAX_COUNT, 1)
    tp.wr(MT_FCE_PDMA_GLOBAL_CONF, FCE_PDMA_GLOBAL_CONF_VALUE)
    tp.wr(MT_FCE_SKIP_FS, FCE_SKIP_FS_VALUE)

    mcu.upload_firmware(fw)
    mcu.mcu_running = True


def mcu_init(tp: MT7601UTransport, mcu: MT7601UMcu,
             image_path: str | Path | None = None) -> bool:
    """mcu.c:504 -- load the image, then mark the MCU running. True when already warm."""
    return load_firmware(tp, mcu, image_path)


def mcu_cmd_init(mcu: MT7601UMcu) -> None:
    """mcu.c:519 -- select the firmware queue, then arm the response endpoint."""
    mcu.function_select(Q_SELECT, 1)