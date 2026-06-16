"""RTL8822BU EFUSE read — HALMAC physical-map dump + 8822b logical parse.

The chip-info probe reads the EFUSE *up front*, before MAC power-on:
``rtl8822b_read_efuse`` -> ``EFUSE_ShadowMapUpdate`` -> the HALMAC driver-side dump
(no FW yet, so the AUTO path falls to ``dump_efuse_drv_88xx``). The physical read is
the only part that touches the wire; the PG-header parse and field decode are pure
computation on the bytes read back.

Wire sequence (de-mirrored):
  R 0x0A            REG_SYS_EEPROM_CTRL — autoload / eeprom-sel flags
  R 0x35            switch_efuse_bank(WIFI): REG_LDO_EFUSE_CTRL+1; bank already 0 -> no write
  R 0x37; W 0x37    cfg_ldo25(0): clear BIT(7) of REG_LDO_EFUSE_CTRL+3 (read needs no 2.5V LDO)
  R 0x30            initial REG_EFUSE_CTRL (preserves the upper command bits across the loop)
  for addr 0..1023: W 0x30 (addr<<8, EF_FLAG clear) ; R 0x30 (poll until EF_FLAG, data = byte)

Ported from:
  [SRC] hal/rtl8822b/rtl8822b_ops.c:616                       rtl8822b_read_efuse
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1507       dump_efuse_drv_88xx
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1089       read_hw_efuse_88xx
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:995        switch_efuse_bank_88xx
  [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_common_8822b.c:159  cfg_ldo25_8822b
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1198       eeprom_parser_88xx
"""
from __future__ import annotations

from dataclasses import dataclass

from typing import Optional

from .constants import (
    BIT_AUTOLOAD_SUS,
    BIT_EERPOMSEL,
    BIT_EF_FLAG,
    BIT_MASK_EF_ADDR,
    BIT_MASK_EF_DATA,
    BIT_SHIFT_EF_ADDR,
    BITS_EF_ADDR,
    EEPROM_CHANNEL_PLAN,
    EEPROM_DEFAULT_CRYSTAL_CAP,
    EEPROM_DEFAULT_THERMAL_METER,
    EEPROM_MAC_ADDR,
    EEPROM_RFE_OPTION,
    EEPROM_SIZE_8822B,
    EEPROM_THERMAL_METER,
    EEPROM_XTAL,
    EFUSE_PA_BIAS,
    EFUSE_SIZE_8822B,
    HALMAC_EFUSE_BANK_WIFI,
    PRTCT_EFUSE_SIZE_8822B,
    REG_EFUSE_CTRL,
    REG_LDO_EFUSE_CTRL,
    REG_SYS_EEPROM_CTRL,
)


@dataclass
class Efuse8822b:
    phy_map: bytes          # the 1024-byte physical EFUSE (raw read order)
    log_map: bytes          # the 768-byte logical map (PG-header decoded)
    autoload_fail: bool     # !BIT_AUTOLOAD_SUS — map is invalid / defaults must be used
    eeprom_or_efuse: bool   # BIT_EERPOMSEL — true => external EEPROM, false => on-chip eFuse
    # Decoded scalar fields (BB/RF/calibration inputs). The PG tx-power block is decoded
    # at the tx-power milestone, where each value can be checked against the writes it drives.
    crystal_cap: int        # [SRC] rtl8822b_ops.c:322 Hal_EfuseParseXtal
    rfe_type: int           # [SRC] rtl8822b_ops.c:515 Hal_ReadRFEType (RF front-end variant)
    thermal_meter: int      # [SRC] rtl8822b_ops.c:335 Hal_EfuseParseThermalMeter
    channel_plan: int       # [SRC] rtl8822b_ops.c:310 (raw efuse byte; plan resolution is registry work)
    mac_address: Optional[str]
    pa_bias: tuple          # efuse[0x3D7], efuse[0x3D8] (PA bias) [SRC] rtl8822b_ops.c:553


def _switch_efuse_bank_wifi(t) -> None:
    """[SRC] halmac_efuse_88xx.c:995 switch_efuse_bank_88xx(WIFI).

    Reads REG_LDO_EFUSE_CTRL+1; the bank lives in bits[1:0]. WIFI is bank 0, and the
    chip powers up on bank 0, so the read matches and the function returns without the
    bank write (which is why the wire shows only one R 0x35 here, no W)."""
    reg = t.read8(REG_LDO_EFUSE_CTRL + 1)
    if (reg & 0x3) == HALMAC_EFUSE_BANK_WIFI:
        return
    reg = (reg & ~0x3) | HALMAC_EFUSE_BANK_WIFI
    t.write8(REG_LDO_EFUSE_CTRL + 1, reg)
    if (t.read8(REG_LDO_EFUSE_CTRL + 1) & 0x3) != HALMAC_EFUSE_BANK_WIFI:
        raise RuntimeError("RTL8822BU: efuse bank switch to WIFI failed")


def _cfg_ldo25(t, enable: bool) -> None:
    """[SRC] halmac_common_8822b.c:159 cfg_ldo25_8822b — toggle the 2.5V EFUSE LDO
    (BIT(7) of REG_LDO_EFUSE_CTRL+3). Reads need no 2.5V LDO, so the read path clears it."""
    v = t.read8(REG_LDO_EFUSE_CTRL + 3)
    v = (v | 0x80) if enable else (v & ~0x80)
    t.write8(REG_LDO_EFUSE_CTRL + 3, v & 0xFF)


def _read_hw_efuse(t, offset: int, size: int) -> bytes:
    """[SRC] halmac_efuse_88xx.c:1089 read_hw_efuse_88xx — the physical byte loop.

    Disable the 2.5V LDO (reads don't need it), latch the initial REG_EFUSE_CTRL so its
    upper command bits carry across the loop, then per byte: write the address with EF_FLAG
    cleared, poll REG_EFUSE_CTRL until EF_FLAG is set, take the low byte as data."""
    _cfg_ldo25(t, enable=False)
    value32 = t.read32(REG_EFUSE_CTRL)
    out = bytearray(size)
    for addr in range(offset, offset + size):
        value32 &= ~(BIT_MASK_EF_DATA | BITS_EF_ADDR)
        value32 |= (addr & BIT_MASK_EF_ADDR) << BIT_SHIFT_EF_ADDR
        t.write32(REG_EFUSE_CTRL, value32 & ~BIT_EF_FLAG)
        while True:
            tmp32 = t.read32(REG_EFUSE_CTRL)
            if tmp32 & BIT_EF_FLAG:
                break
        out[addr - offset] = tmp32 & BIT_MASK_EF_DATA
    return bytes(out)


def _eeprom_parser(phy_map: bytes) -> bytes:
    """[SRC] halmac_efuse_88xx.c:1198 eeprom_parser_88xx — PG-header physical -> logical.

    The physical map is a sequence of PG blocks. Each block has a 1- or 2-byte header
    (the 2-byte extended form when hdr[4:0]==0x0f) giving a block index and a 4-bit
    word-enable mask; each *enabled* word copies two bytes to logical offset
    (blk_idx<<3)+(word<<1). A 0xFF header ends the walk. The walk is bounded by
    efuse_size - prtct_efuse_size to mirror the source's overrun guards."""
    log = bytearray(b"\xff" * EEPROM_SIZE_8822B)
    limit = EFUSE_SIZE_8822B - PRTCT_EFUSE_SIZE_8822B
    idx = 0
    while True:
        hdr = phy_map[idx]
        if hdr == 0xFF:
            break
        if (hdr & 0x1F) == 0x0F:
            idx += 1
            hdr2 = phy_map[idx]
            if hdr2 == 0xFF:
                break
            blk_idx = ((hdr2 & 0xF0) >> 1) | ((hdr >> 5) & 0x07)
            word_en = hdr2 & 0x0F
        else:
            blk_idx = (hdr & 0xF0) >> 4
            word_en = hdr & 0x0F
        idx += 1
        if idx >= limit - 1:
            raise ValueError("RTL8822BU: efuse PG parse overran the protected boundary")
        for i in range(4):
            if (word_en >> i) & 0x1:        # word_en bit set => that word is NOT written
                continue
            eeprom_idx = (blk_idx << 3) + (i << 1)
            if eeprom_idx + 1 > EEPROM_SIZE_8822B:
                raise ValueError("RTL8822BU: efuse PG block index out of logical range")
            log[eeprom_idx] = phy_map[idx]
            idx += 1
            log[eeprom_idx + 1] = phy_map[idx]
            idx += 1
    return bytes(log)


def _scalar(log_map: bytes, off: int, default: int, valid: bool) -> int:
    """Efuse byte with the vendor's blank/invalid-map fallback (0xFF or !valid -> default).
    [SRC] rtl8822b_ops.c Hal_EfuseParseXtal/ThermalMeter pattern."""
    v = log_map[off]
    return v if (valid and v != 0xFF) else default


def _mac_address(log_map: bytes, valid: bool) -> Optional[str]:
    mac = log_map[EEPROM_MAC_ADDR:EEPROM_MAC_ADDR + 6]
    if not valid or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def read_phydm_trim(t) -> None:
    """rtw_phydm_read_efuse [SRC] hal_dm.c -> phydm_get_thermal_trim_offset_8822b +
    phydm_get_power_trim_offset_8822b [SRC] halrf_kfree.c — read the thermal + 2G/5G power-trim
    PG bytes (PPG_THERMAL, PPG_2G_TXAB, PPG_5GL1_TXA). On this card those PG bytes are blank (0xFF),
    so each is one odm_efuse_one_byte_read served from the cached physical map — only the WIFI
    bank-switch (R 0x35) reaches the wire, three times. Runs at the tail of read_chip_info, after
    hal_read_mac_hidden_rpt's power-off."""
    for _ in range(3):
        _switch_efuse_bank_wifi(t)


def efuse_one_byte_read(t, phy_map: bytes, addr: int) -> int:
    """[SRC] odm_efuse_one_byte_read — a single PG byte served from the cached physical map.

    The byte itself comes from `phy_map` (already dumped at read_efuse), so the only wire op is the
    WIFI bank-switch (one R 0x35; bank is already 0 ⇒ no write). PHYDM's PG readers (kfree / pa-bias)
    call this per byte, so each shows up as one 0x35 read [SRC] halmac read_physical_efuse_map."""
    _switch_efuse_bank_wifi(t)
    return phy_map[addr]


def read_efuse(t) -> Efuse8822b:
    """rtl8822b_read_efuse: autoload-flag read + the HALMAC driver-side physical dump,
    then PG-header decode to the logical map and the scalar field decode.
    [SRC] rtl8822b_ops.c:616."""
    val8 = t.read8(REG_SYS_EEPROM_CTRL)              # R 0x0A
    eeprom_or_efuse = bool(val8 & BIT_EERPOMSEL)
    autoload_fail = not (val8 & BIT_AUTOLOAD_SUS)

    _switch_efuse_bank_wifi(t)
    phy_map = _read_hw_efuse(t, 0, EFUSE_SIZE_8822B)
    log_map = _eeprom_parser(phy_map)

    # Hal_EfuseParsePABias reads physical efuse 0x3D7/0x3D8 via rtw_efuse_access. The physical
    # map is already cached (valid) from the dump, so HALMAC's read_physical_efuse_map serves it
    # from cache — the only wire op is the WIFI bank-switch read, no 0x30 loop.
    # [SRC] rtl8822b_ops.c:553 Hal_EfuseParsePABias, halmac_efuse_88xx.c:132 dump_efuse_map_88xx.
    _switch_efuse_bank_wifi(t)
    pa_bias = (phy_map[EFUSE_PA_BIAS], phy_map[EFUSE_PA_BIAS + 1])

    valid = not autoload_fail
    # rfe_type: registry default is "use efuse" (CONFIG_RTW_RFE_TYPE sentinel), so the efuse
    # byte wins; a blank byte falls back to 0. [SRC] rtl8822b_ops.c:515 Hal_ReadRFEType.
    rfe = log_map[EEPROM_RFE_OPTION]
    rfe_type = rfe if (valid and rfe != 0xFF) else 0
    return Efuse8822b(
        phy_map=phy_map, log_map=log_map,
        autoload_fail=autoload_fail, eeprom_or_efuse=eeprom_or_efuse,
        crystal_cap=_scalar(log_map, EEPROM_XTAL, EEPROM_DEFAULT_CRYSTAL_CAP, valid),
        rfe_type=rfe_type,
        thermal_meter=_scalar(log_map, EEPROM_THERMAL_METER, EEPROM_DEFAULT_THERMAL_METER, valid),
        channel_plan=log_map[EEPROM_CHANNEL_PLAN],
        mac_address=_mac_address(log_map, valid),
        pa_bias=pa_bias,
    )
