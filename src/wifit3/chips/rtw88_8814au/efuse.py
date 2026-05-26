"""RTL8814AU EFUSE read — pins rfe_option, MAC address, crystal_cap.

Ports rtw8814a_efuse_grant + rtw8814a_read_efuse (rtw8814a.c) plus the generic
physical-read + word-enable de-map (efuse.c). The de-map walker and the
low-level physical read are family-generic (same as the 8812au port); the
8814a specifics are:

  * physical EFUSE is 1024 bytes (.phy_efuse_size), logical map 512
    (.log_efuse_size)
  * logical-map field offsets are `struct rtw8814a_efuse` (rtw8814a.h):
      xtal_k 0xB9, rf_board_option 0xC1, rfe_option 0xCA, and the USB MAC at
      0xD8 (struct rtw8814au_efuse: vid 0xD0, pid 0xD2, res 0xD4, mac 0xD8)
  * rfe_option resolution (rtw8814a_read_rfe_type): if bit 7 is set, USB maps
    it to 1; otherwise the raw byte stands.

References:
    rtw8814a.c   rtw8814a_efuse_grant / rtw8814a_read_efuse / read_rfe_type
    efuse.c      rtw_dump_physical_efuse_map / rtw_dump_logical_efuse_map
    phy.c:1113   cond.rfe = efuse->rfe_option
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)

# --- register addresses (reg.h, family-generic) ---------------------------
REG_EFUSE_CTRL = 0x0030
REG_LDO_EFUSE_CTRL = 0x0034
REG_EFUSE_ACCESS = 0x00CF
REG_SYS_FUNC_EN = 0x0002
REG_SYS_CLKR = 0x0008

BIT_EF_FLAG = 1 << 31
BIT_SHIFT_EF_ADDR = 8
BIT_MASK_EF_ADDR = 0x3FF                 # 10-bit address (supports 1024 B)
BITS_EF_ADDR = BIT_MASK_EF_ADDR << BIT_SHIFT_EF_ADDR
BIT_MASK_EF_DATA = 0xFF
BIT_MASK_EFUSE_BANK_SEL = (1 << 8) | (1 << 9)

EFUSE_ACCESS_ON = 0x69
EFUSE_ACCESS_OFF = 0x00
BIT_FEN_ELDR = 1 << 12
BIT_LOADER_CLK_EN = 1 << 5
BIT_ANA8M = 1 << 1

# 8814a sizes (rtw8814a_hw_spec)
EFUSE_PHYSICAL_SIZE = 1024
EFUSE_LOGICAL_SIZE = 512
EFUSE_PROTECT_SIZE = 0

# --- logical-map offsets (struct rtw8814a_efuse, rtw8814a.h) ---------------
OFF_CHANNEL_PLAN = 0xB8
OFF_XTAL_K = 0xB9
OFF_RF_BOARD_OPTION = 0xC1
OFF_TRX_ANTENNA_OPTION = 0xC9
OFF_RFE_OPTION = 0xCA
OFF_COUNTRY_CODE = 0xCB
OFF_MAC_ADDR_8814AU = 0xD8        # struct rtw8814au_efuse: mac_addr @ 0xD8


@dataclass(frozen=True)
class EfuseRead:
    crystal_cap: int          # xtal_k & 0x3F (0xFF → 0x20 default)
    rfe_option: int           # resolved (read_rfe_type)
    rfe_option_raw: int       # raw EFUSE byte
    rf_board_option: int
    trx_antenna_option: int
    channel_plan: int
    mac_addr: bytes           # 6 bytes


def _efuse_grant(transport: RTL8814AUTransport, on: bool) -> None:
    """rtw8814a_efuse_grant (rtw8814a.c)."""
    if on:
        transport.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ON)
        transport.write16(REG_SYS_FUNC_EN,
                          transport.read16(REG_SYS_FUNC_EN) | BIT_FEN_ELDR)
        transport.write16(REG_SYS_CLKR,
                          transport.read16(REG_SYS_CLKR)
                          | BIT_LOADER_CLK_EN | BIT_ANA8M)
    else:
        transport.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)


def _switch_efuse_bank_wifi(transport: RTL8814AUTransport) -> None:
    cur = transport.read32(REG_LDO_EFUSE_CTRL)
    transport.write32(REG_LDO_EFUSE_CTRL, (cur & ~BIT_MASK_EFUSE_BANK_SEL) & 0xFFFFFFFF)


def read8_physical_efuse(transport: RTL8814AUTransport, addr: int,
                         timeout_s: float = 0.1) -> int:
    """rtw_read8_physical_efuse (efuse.c)."""
    transport.write32_mask(REG_EFUSE_CTRL, BITS_EF_ADDR, addr & BIT_MASK_EF_ADDR)
    transport.write32_clr(REG_EFUSE_CTRL, BIT_EF_FLAG)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = transport.read32(REG_EFUSE_CTRL)
        if v & BIT_EF_FLAG:
            return v & BIT_MASK_EF_DATA
        time.sleep(0.0005)
    raise IOError(f"EFUSE read at addr 0x{addr:03x} timed out")


def dump_physical_efuse_map(transport: RTL8814AUTransport,
                            size: int = EFUSE_PHYSICAL_SIZE) -> bytes:
    return bytes(read8_physical_efuse(transport, a) for a in range(size))


def _hdr_invalid(hdr1: int, hdr2: int) -> bool:
    return hdr1 == 0xFF or ((hdr1 & 0x1F) == 0xF and hdr2 == 0xFF)


def parse_logical_efuse_map(phy_map: bytes,
                            logical_size: int = EFUSE_LOGICAL_SIZE,
                            protect_size: int = EFUSE_PROTECT_SIZE) -> bytes:
    """rtw_dump_logical_efuse_map (efuse.c) — word-enable header walker."""
    log_map = bytearray([0xFF] * logical_size)
    physical_size = len(phy_map)

    phy_idx = 0
    while phy_idx < physical_size - protect_size:
        hdr1 = phy_map[phy_idx]
        if phy_idx + 1 >= physical_size - protect_size:
            break
        hdr2 = phy_map[phy_idx + 1]
        if _hdr_invalid(hdr1, hdr2):
            break

        if (hdr1 & 0x1F) == 0xF:
            blk_idx = ((hdr2 & 0xF0) >> 1) | ((hdr1 >> 5) & 0x07)
            word_en = hdr2 & 0xF
            phy_idx += 2
        else:
            blk_idx = (hdr1 & 0xF0) >> 4
            word_en = hdr1 & 0xF
            phy_idx += 1

        for i in range(4):
            if word_en & (1 << i):
                continue
            log_idx = (blk_idx << 3) + (i << 1)
            if phy_idx + 1 > physical_size - protect_size or log_idx + 1 > logical_size:
                logger.warning("EFUSE: walker hit bounds at phy=%d log=%d",
                               phy_idx, log_idx)
                return bytes(log_map)
            log_map[log_idx] = phy_map[phy_idx]
            log_map[log_idx + 1] = phy_map[phy_idx + 1]
            phy_idx += 2

    return bytes(log_map)


def _resolve_rfe_option(raw: int) -> int:
    """rtw8814a_read_rfe_type: bit7 set → USB maps to 1; else raw byte."""
    if raw & (1 << 7):
        return 1   # USB
    return raw


def read_efuse(transport: RTL8814AUTransport) -> EfuseRead:
    """Grant + physical dump (1024 B) + de-map (512 B) + parse 8814a fields."""
    logger.info("EFUSE: granting access + dumping %d physical bytes...",
                EFUSE_PHYSICAL_SIZE)
    _efuse_grant(transport, on=True)
    try:
        _switch_efuse_bank_wifi(transport)
        t0 = time.monotonic()
        phy_map = dump_physical_efuse_map(transport)
        logger.info("EFUSE: physical dump done in %.0f ms",
                    (time.monotonic() - t0) * 1000)
    finally:
        _efuse_grant(transport, on=False)

    log_map = parse_logical_efuse_map(phy_map)

    xtal = log_map[OFF_XTAL_K]
    crystal_cap = 0x20 if xtal == 0xFF else (xtal & 0x3F)
    rfe_raw = log_map[OFF_RFE_OPTION]

    return EfuseRead(
        crystal_cap=crystal_cap,
        rfe_option=_resolve_rfe_option(rfe_raw),
        rfe_option_raw=rfe_raw,
        rf_board_option=log_map[OFF_RF_BOARD_OPTION],
        trx_antenna_option=log_map[OFF_TRX_ANTENNA_OPTION],
        channel_plan=log_map[OFF_CHANNEL_PLAN],
        mac_addr=bytes(log_map[OFF_MAC_ADDR_8814AU:OFF_MAC_ADDR_8814AU + 6]),
    )
