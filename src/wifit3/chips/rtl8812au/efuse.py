"""RTL8812AU EFUSE read.

Replaces the hardcoded :class:`EfuseDefaults` with values actually burned
into the chip — `rfe_option`, `pa_type`, `lna_type_2g/5g`, `crystal_cap`,
and the MAC address. Without this, an AWUS036ACH-style high-power card
runs as if it had only the internal LNA, killing sensitivity below
~-50 dBm.

Reference (rtw88-source-v6.18):
    efuse.c:14        switch_efuse_bank
    efuse.c:40        rtw_dump_logical_efuse_map (header-based walker)
    efuse.c:87        rtw_dump_physical_efuse_map (per-byte read loop)
    efuse.c:125       rtw_read8_physical_efuse
    rtw88xxa.c:18     rtw88xxa_efuse_grant
    rtw88xxa.c:32     rtw8812a_read_amplifier_type
    rtw88xxa.c:80     rtw8812a_read_rfe_type
    rtw88xxa.c:200    rtw88xxa_read_efuse
    rtw88xxa.h:28..62 struct rtw88xxa_efuse layout

The chip's EFUSE is 512 physical bytes (rtw8812a.c:1047). The logical
map is also 512 bytes but encoded compactly via 1-byte or 2-byte word
headers — see the kernel walker. After parsing the logical map we index
specific offsets per `struct rtw88xxa_efuse`.

Logical offsets (per `struct rtw88xxa_efuse` in rtw88xxa.h):
    0xB8  channel_plan
    0xB9  xtal_k                 ← crystal_cap
    0xBC  pa_type                ← both 2g + 5g
    0xBD  lna_type_2g
    0xBF  lna_type_5g
    0xC1  rf_board_option        ← btcoex hint for 8812A
    0xCA  rfe_option             ← THE KEY field for sensitivity
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .transport import RTL8812AUTransport

logger = logging.getLogger(__name__)


# --- register addresses (reg.h) -------------------------------------------
REG_EFUSE_CTRL = 0x0030
REG_LDO_EFUSE_CTRL = 0x0034
REG_EFUSE_ACCESS = 0x00CF
REG_SYS_FUNC_EN = 0x0002
REG_SYS_CLKR = 0x0008

# REG_EFUSE_CTRL bitfields
BIT_EF_FLAG = 1 << 31                 # write to clear → trigger read; reads back set on completion
BIT_SHIFT_EF_ADDR = 8
BIT_MASK_EF_ADDR = 0x3FF              # 10-bit address (supports up to 1024B)
BITS_EF_ADDR = BIT_MASK_EF_ADDR << BIT_SHIFT_EF_ADDR  # 0x3FF00
BIT_MASK_EF_DATA = 0xFF

# REG_LDO_EFUSE_CTRL: bank-select bits (bits 8..9), 0 = WIFI bank
BIT_MASK_EFUSE_BANK_SEL = (1 << 8) | (1 << 9)

# REG_EFUSE_ACCESS magic byte
EFUSE_ACCESS_ON = 0x69
EFUSE_ACCESS_OFF = 0x00

# REG_SYS_FUNC_EN bits
BIT_FEN_ELDR = 1 << 12

# REG_SYS_CLKR bits (at byte 1 of REG_SYS_CLKR == 0x09 byte address)
BIT_LOADER_CLK_EN = 1 << 5
BIT_ANA8M = 1 << 1

# EFUSE sizes for 8812A (rtw8812a.c hw_spec)
EFUSE_PHYSICAL_SIZE = 512
EFUSE_LOGICAL_SIZE = 512
EFUSE_PROTECT_SIZE = 0


# --- logical-map offsets (per struct rtw88xxa_efuse) ----------------------
OFF_CHANNEL_PLAN = 0xB8
OFF_XTAL_K = 0xB9
OFF_THERMAL_METER = 0xBA
OFF_PA_TYPE = 0xBC
OFF_LNA_TYPE_2G = 0xBD
OFF_LNA_TYPE_5G = 0xBF
OFF_RF_BOARD_OPTION = 0xC1
OFF_RF_ANTENNA_OPTION = 0xC9
OFF_RFE_OPTION = 0xCA
OFF_COUNTRY_CODE = 0xCB
# MAC address varies by chip in the union at end of struct. For rtw8812au
# (per rtw8812au_efuse in rtw88xxa.h) the addr is the FIRST 6 bytes of the
# 8812au sub-struct, which starts at offset 0xD0.
OFF_8812AU_MAC_ADDR = 0xD0


@dataclass(frozen=True)
class EfuseRead:
    """Subset of the EFUSE map we currently care about for bring-up."""
    crystal_cap: int       # xtal_k, 0..0x3F (0xFF → falls back to 0x20)
    pa_type: int           # raw byte
    lna_type_2g: int       # raw byte
    lna_type_5g: int       # raw byte
    rfe_option_raw: int    # raw byte from EFUSE
    rf_board_option: int   # raw byte (btcoex hint at bits 5..7)
    mac_addr: bytes        # 6 bytes
    # Derived (computed by classify_8812a()):
    ext_pa_2g: int         # 0/1
    ext_lna_2g: int        # 0/1
    ext_pa_5g: int         # 0/1
    ext_lna_5g: int        # 0/1
    rfe_option: int        # resolved 0..6 (after rtw8812a_read_rfe_type)
    btcoex: bool
    thermal_meter: int = 0xFF   # raw byte (0xFF = uncalibrated); pwr-track reference


# --- low-level: EFUSE access grant ----------------------------------------
def _efuse_grant(transport: RTL8812AUTransport, on: bool) -> None:
    """Port of `rtw88xxa_efuse_grant` (rtw88xxa.c:18)."""
    if on:
        transport.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ON)
        # REG_SYS_FUNC_EN bit 12 → set BIT_FEN_ELDR via 16-bit set
        cur16 = transport.read16(REG_SYS_FUNC_EN)
        transport.write16(REG_SYS_FUNC_EN, cur16 | BIT_FEN_ELDR)
        # REG_SYS_CLKR+1 set BIT_LOADER_CLK_EN | BIT_ANA8M
        # Wait — kernel writes `REG_SYS_CLK_CTRL + 1` which is at offset 9.
        # BIT_LOADER_CLK_EN = BIT(5) of REG_SYS_CLKR (offset 0x08).
        # BIT_ANA8M = BIT(1) of REG_SYS_CLKR (offset 0x08).
        # So both bits are in byte 0 of REG_SYS_CLKR (offset 0x08, NOT +1).
        # But kernel writes BIT(13) and BIT(9) of REG_SYS_CLK_CTRL+1.
        # Re-reading: REG_SYS_CLKR is 16-bit. BIT_LOADER_CLK_EN = BIT(5) of
        # the *word* at REG_SYS_CLKR (= byte 0 of the 16-bit reg), and
        # BIT_ANA8M = BIT(1). Same byte. OK so a single 8-bit set on byte 0.
        transport.write8_set(REG_SYS_CLKR, BIT_LOADER_CLK_EN | BIT_ANA8M)
    else:
        transport.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)


def _switch_efuse_bank_wifi(transport: RTL8812AUTransport) -> None:
    """Port of `switch_efuse_bank` (efuse.c:14). Bank 0 = WIFI."""
    cur = transport.read32(REG_LDO_EFUSE_CTRL)
    new = (cur & ~BIT_MASK_EFUSE_BANK_SEL)   # bank = 0
    transport.write32(REG_LDO_EFUSE_CTRL, new & 0xFFFFFFFF)


def read8_physical_efuse(transport: RTL8812AUTransport, addr: int,
                         timeout_s: float = 0.1) -> int:
    """Port of `rtw_read8_physical_efuse` (efuse.c:125).

    Writes the 10-bit address into REG_EFUSE_CTRL bits 8..17, clears the
    EF_FLAG bit, polls until the chip sets EF_FLAG back (signals data
    ready), then reads the data byte at REG_EFUSE_CTRL byte 0.
    """
    # write addr → bits 8..17 of REG_EFUSE_CTRL
    transport.write32_mask(REG_EFUSE_CTRL, BITS_EF_ADDR, addr & BIT_MASK_EF_ADDR)
    # clear BIT_EF_FLAG to trigger
    transport.write32_clr(REG_EFUSE_CTRL, BIT_EF_FLAG)
    # poll for completion
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = transport.read32(REG_EFUSE_CTRL)
        if v & BIT_EF_FLAG:
            return v & BIT_MASK_EF_DATA
        time.sleep(0.0005)
    raise IOError(f"EFUSE read at addr 0x{addr:03x} timed out")


def dump_physical_efuse_map(transport: RTL8812AUTransport,
                            size: int = EFUSE_PHYSICAL_SIZE) -> bytes:
    """Port of `rtw_dump_physical_efuse_map` (efuse.c:87).

    Returns the raw `size`-byte physical EFUSE contents. Caller is
    responsible for `_efuse_grant(on=True)` + `_switch_efuse_bank_wifi`
    before, and `_efuse_grant(on=False)` after.
    """
    out = bytearray(size)
    for addr in range(size):
        out[addr] = read8_physical_efuse(transport, addr)
    return bytes(out)


# --- logical map walker (mirrors efuse.c:40 rtw_dump_logical_efuse_map) ---

def _hdr_invalid(hdr1: int, hdr2: int) -> bool:
    return hdr1 == 0xFF or ((hdr1 & 0x1F) == 0xF and hdr2 == 0xFF)


def parse_logical_efuse_map(phy_map: bytes,
                            logical_size: int = EFUSE_LOGICAL_SIZE,
                            protect_size: int = EFUSE_PROTECT_SIZE) -> bytes:
    """Walk the physical EFUSE map to construct the logical map.

    EFUSE word layout (per efuse.c comment lines 31..39):
        1-byte header: blk_idx[6:3]=0, blk_idx[2:0]=hdr1[7:5], word_en=hdr1[3:0]
                       (used when hdr1[4:0] != 0xF)
        2-byte header: blk_idx[6:3]=hdr2[7:4], blk_idx[2:0]=hdr1[7:5],
                       word_en=hdr2[3:0]
                       (used when hdr1[4:0] == 0xF)
        word_en: 4 bits, one per word. 0 = written, 1 = skipped.
        Each "word" = 2 bytes.
        Each block = up to 4 words (8 bytes).

    Logical offset of word `i` in block `blk_idx` = (blk_idx << 3) + (i << 1).
    """
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
            # 2-byte header
            blk_idx = ((hdr2 & 0xF0) >> 1) | ((hdr1 >> 5) & 0x07)
            word_en = hdr2 & 0xF
            phy_idx += 2
        else:
            # 1-byte header
            blk_idx = (hdr1 & 0xF0) >> 4
            word_en = hdr1 & 0xF
            phy_idx += 1

        for i in range(4):
            if word_en & (1 << i):
                continue  # this word skipped
            log_idx = (blk_idx << 3) + (i << 1)
            if phy_idx + 1 > physical_size - protect_size or log_idx + 1 > logical_size:
                logger.warning("EFUSE: walker hit bounds at phy=%d log=%d", phy_idx, log_idx)
                return bytes(log_map)
            log_map[log_idx] = phy_map[phy_idx]
            log_map[log_idx + 1] = phy_map[phy_idx + 1]
            phy_idx += 2

    return bytes(log_map)


# --- classification: derive ext_pa/ext_lna/rfe_option (rtw88xxa.c:32, :80) -

def _classify_amplifier(pa_type: int,
                        lna_type_2g: int, lna_type_5g: int) -> dict[str, int]:
    """Port of `rtw8812a_read_amplifier_type` (rtw88xxa.c:32..78)."""
    # efuse->pa_type_2g = efuse->pa_type_5g = map->pa_type (kernel
    # assigns the same byte to both fields, then derives ext_pa flags).
    pa_type_2g = pa_type
    pa_type_5g = pa_type
    return {
        "ext_pa_2g": int(bool(pa_type_2g & (1 << 5)) and bool(pa_type_2g & (1 << 4))),
        "ext_lna_2g": int(bool(lna_type_2g & (1 << 7)) and bool(lna_type_2g & (1 << 3))),
        "ext_pa_5g": int(bool(pa_type_5g & (1 << 1)) and bool(pa_type_5g & (1 << 0))),
        "ext_lna_5g": int(bool(lna_type_5g & (1 << 7)) and bool(lna_type_5g & (1 << 3))),
    }


def _resolve_rfe_option(rfe_option_raw: int, ext: dict[str, int]) -> int:
    """Port of `rtw8812a_read_rfe_type` (rtw88xxa.c:80..122).

    Resolves the final rfe_option that drives `_phy_set_rfe_reg_24g_8812a`.
    HCI is always USB for us, so the unset-EFUSE default is 0.
    """
    if rfe_option_raw == 0xFF:
        return 0   # USB default when EFUSE empty
    if rfe_option_raw & (1 << 7):
        # Special bit-7 interpretation: derive from ext flags.
        if ext["ext_lna_5g"]:
            if ext["ext_pa_5g"]:
                if ext["ext_lna_2g"] and ext["ext_pa_2g"]:
                    return 3
                return 0
            return 2
        return 4
    rfe_option = rfe_option_raw & 0x3F
    # Workaround for older-customer-EFUSE: rfe=4 + any ext_* → force USB=0.
    if rfe_option == 4 and (
        ext["ext_pa_5g"] or ext["ext_pa_2g"]
        or ext["ext_lna_5g"] or ext["ext_lna_2g"]
    ):
        return 0
    return rfe_option


# --- public entry point ---------------------------------------------------

def read_efuse_8812a(transport: RTL8812AUTransport) -> EfuseRead:
    """Read + parse + classify the 8812A EFUSE in one shot.

    Performs:
      1. Grant EFUSE access (REG_EFUSE_ACCESS + REG_SYS_FUNC_EN/CLKR bits).
      2. Switch to WIFI bank.
      3. Dump 512 physical bytes.
      4. Walk to construct the 512-byte logical map.
      5. Extract specific fields (crystal_cap, pa/lna types, rfe_option_raw,
         MAC address).
      6. Apply rtw8812a_read_amplifier_type + rtw8812a_read_rfe_type
         resolution.
      7. Revoke EFUSE access.

    Returns an :class:`EfuseRead` with both raw and derived fields.
    Raises IOError on EFUSE read timeout.
    """
    logger.debug("EFUSE: granting access...")
    _efuse_grant(transport, on=True)
    try:
        _switch_efuse_bank_wifi(transport)
        logger.debug("EFUSE: dumping %d physical bytes...", EFUSE_PHYSICAL_SIZE)
        t0 = time.monotonic()
        phy_map = dump_physical_efuse_map(transport)
        logger.debug("EFUSE: physical dump done in %.0f ms", (time.monotonic() - t0) * 1000)
    finally:
        _efuse_grant(transport, on=False)

    log_map = parse_logical_efuse_map(phy_map)

    crystal_cap = log_map[OFF_XTAL_K] & 0x3F
    if log_map[OFF_XTAL_K] == 0xFF:
        crystal_cap = 0x20   # kernel default when EFUSE unset
    pa_type = log_map[OFF_PA_TYPE]
    lna_type_2g = log_map[OFF_LNA_TYPE_2G]
    lna_type_5g = log_map[OFF_LNA_TYPE_5G]
    rfe_option_raw = log_map[OFF_RFE_OPTION]
    rf_board_option = log_map[OFF_RF_BOARD_OPTION]
    thermal_meter = log_map[OFF_THERMAL_METER]
    mac_addr = bytes(log_map[OFF_8812AU_MAC_ADDR:OFF_8812AU_MAC_ADDR + 6])

    ext = _classify_amplifier(pa_type, lna_type_2g, lna_type_5g)
    rfe_option = _resolve_rfe_option(rfe_option_raw, ext)

    # btcoex: bits 5..7 of rf_board_option == 0x20 (per rtw88xxa.c:247).
    btcoex = (rf_board_option & 0xE0) == 0x20

    out = EfuseRead(
        crystal_cap=crystal_cap,
        pa_type=pa_type,
        lna_type_2g=lna_type_2g,
        lna_type_5g=lna_type_5g,
        rfe_option_raw=rfe_option_raw,
        rf_board_option=rf_board_option,
        thermal_meter=thermal_meter,
        mac_addr=mac_addr,
        ext_pa_2g=ext["ext_pa_2g"],
        ext_lna_2g=ext["ext_lna_2g"],
        ext_pa_5g=ext["ext_pa_5g"],
        ext_lna_5g=ext["ext_lna_5g"],
        rfe_option=rfe_option,
        btcoex=btcoex,
    )
    logger.info(
        "EFUSE: rfe_option=%d (raw=0x%02x)  ext_pa_2g=%d ext_lna_2g=%d  "
        "ext_pa_5g=%d ext_lna_5g=%d  xtal_k=0x%02x  btcoex=%s  "
        "mac=%s",
        out.rfe_option, out.rfe_option_raw,
        out.ext_pa_2g, out.ext_lna_2g, out.ext_pa_5g, out.ext_lna_5g,
        out.crystal_cap, out.btcoex,
        ":".join(f"{b:02x}" for b in out.mac_addr),
    )
    return out


def efuse_defaults_from_read(read: EfuseRead, rf_path_num: int = 2):
    """Build a :class:`phy.EfuseDefaults` populated from a real EFUSE read."""
    from .phy import EfuseDefaults
    return EfuseDefaults(
        cut=15,
        rfe_option=read.rfe_option,
        btcoex=read.btcoex,
        ant_div_cfg=0,
        ext_lna_2g=read.ext_lna_2g,
        ext_pa_2g=read.ext_pa_2g,
        ext_lna_5g=read.ext_lna_5g,
        ext_pa_5g=read.ext_pa_5g,
        crystal_cap=read.crystal_cap,
        tx_bb_swing_2g=0,
        rf_path_num=rf_path_num,
    )
