"""RTL8188EUS IOL (initial-offload) engine + efuse patch.

The 8188e build runs with ``rtw_fw_iol = 1`` (IOL always enabled), so the MCU
performs efuse reads / LLT init / efuse patches as offloaded command lists rather
than the host doing byte-by-byte register I/O. The host primitives are:

  ``iol_mode_enable`` [SRC] rtl8188e_hal_init.c:26 — toggle SW_OFFLOAD_EN in
  REG_SYS_CFG (0xF0[7]).
  ``iol_execute``     [SRC] rtl8188e_hal_init.c:49 — write the command bits to
  REG_HMEBOX_E0 (0x88), poll until they clear, then check the matching error bit.

``iol_efuse_patch`` (the HAL_INIT_STAGES_EFUSE_PATCH stage) runs READ_EFUSE_MAP
then EFUSE_PATCH. The probe-phase efuse map read (which recovers crystal_cap /
tx-power / MAC) additionally reads the map back from the TX packet buffer — that
is a later milestone; this module ports the IOL core + patch first.
"""
from __future__ import annotations

from typing import NamedTuple

from .constants import (
    ANA8M,
    CMD_EFUSE_PATCH,
    CMD_READ_EFUSE_MAP,
    DEFAULT_CRYSTAL_CAP,
    DISABLE_TRXPKT_BUF_ACCESS,
    EEPROM_MAC_ADDR_88EU,
    EEPROM_RFE_OPTION_88E,
    EEPROM_TX_PWR_INX_88E,
    EEPROM_XTAL_88E,
    EFUSE_ACCESS_OFF,
    EFUSE_ACCESS_ON,
    EFUSE_MAP_LEN_88E,
    EFUSE_MAX_SECTION_88E,
    EFUSE_MAX_WORD_UNIT,
    EFUSE_REAL_CONTENT_LEN_88E,
    FEN_ELDR,
    LOADER_CLK_EN,
    REG_9346CR,
    REG_HMEBOX_E0,
    REG_PKT_BUFF_ACCESS_CTRL,
    REG_PKTBUF_DBG_ADDR,
    REG_PKTBUF_DBG_DATA_H,
    REG_PKTBUF_DBG_DATA_L,
    REG_EFUSE_ACCESS,
    REG_SYS_CFG,
    REG_SYS_CLKR,
    REG_SYS_FUNC_EN,
    REG_TDECTRL,
    REG_TXPKTBUF_DBG,
    SW_OFFLOAD_EN,
    TXPKT_BUF_SELECT,
)
from .firmware import _8051_reset


def read_adapter_info(t) -> None:
    """Probe-time chip-info read, before power-on [SRC] read_adapter_info_8188eu (USB).

    Three steps the kernel runs at USB probe, ahead of ``_InitPowerOn``:
      * ``read_chip_version_8188e`` — read REG_SYS_CFG (cut/vendor/IC type) [rtl8188e_hal_init.c:2451]
      * ``GetEEPROMSize8188E`` + ``_ReadPROMContent`` — read REG_9346CR (boot-from-EEPROM vs
        E-Fuse, autoload OK) [rtl8188e_hal_init.c:2800, usb_halinit.c:2089]
      * ``hal_EfusePowerSwitch_RTL8188E(bWrite=_FALSE, PwrState=_TRUE)`` — REG_EFUSE_ACCESS=ON,
        then make the e-fuse loader reset + clock valid [rtl8188e_hal_init.c:1080]

    The SYS_FUNC_EN / SYS_CLKR steps are read-modify-write that only write when a required bit
    is clear; on this card those bits are already set, so the wire shows reads only. All reads
    feed software state (version/autoload); nothing here gates on hardware we don't have."""
    t.read32(REG_SYS_CFG)                       # read_chip_version_8188e
    t.read16(REG_9346CR)                        # GetEEPROMSize8188E: BOOT_FROM_EEPROM?
    t.read8(REG_9346CR)                         # _ReadPROMContent: boot-select + autoload flag
    # hal_EfusePowerSwitch_RTL8188E(_FALSE, _TRUE)
    t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_ON)
    fn = t.read16(REG_SYS_FUNC_EN)
    if not (fn & FEN_ELDR):
        t.write16(REG_SYS_FUNC_EN, fn | FEN_ELDR)
    clk = t.read16(REG_SYS_CLKR)
    if (not (clk & LOADER_CLK_EN)) or (not (clk & ANA8M)):
        t.write16(REG_SYS_CLKR, clk | LOADER_CLK_EN | ANA8M)

_IOL_POLL_CAP = 100000   # generous bound; the captured poll always converges


class TxPwr2G(NamedTuple):
    """Path-A 2.4 GHz PG TX-power info (1T1R). [SRC] hal_load_pg_txpwr_info_path_2g."""
    cck_base: tuple    # 6 CCK channel groups (cck_group index)
    bw40_base: tuple   # 5 BW40 channel groups (also the OFDM/HT base)
    cck_diff: int      # CCK 1TX diff (no efuse byte -> 0)
    ofdm_diff: int     # OFDM 1TX diff
    bw20_diff: int     # BW20 (HT) 1TX diff


class BoardOptions(NamedTuple):
    """RFE/amplifier board options decoded from efuse 0xCA (the AUTO / registry-default
    path). [SRC] Hal_ReadPAType_8188E + Hal_ReadAmplifierType_8188E."""
    external_pa_2g: bool
    external_lna_2g: bool
    type_glna: int      # ext-LNA gain-table select fed to phydm (0x0/0x1/0x2)


class ChipParams(NamedTuple):
    crystal_cap: int
    mac_address: bytes
    efuse_map: bytes   # the 512-byte logical map
    tx_power: TxPwr2G  # path-A 2.4G TX-power PG decode
    board: BoardOptions  # external PA/LNA board options (drive the phydm table walk + RFE)


def iol_mode_enable(t, enable: bool, fw_ready: bool = True) -> None:
    """Toggle initial-offload (SW_OFFLOAD_EN) in REG_SYS_CFG. [SRC] iol_mode_enable.

    When enabling before the FW is up (the probe-phase efuse read), the vendor
    resets the 8051 first; post-M1 (efuse patch) the FW is ready and it is skipped."""
    reg = t.read8(REG_SYS_CFG)
    if enable:
        t.write8(REG_SYS_CFG, reg | SW_OFFLOAD_EN)
        if not fw_ready:
            _8051_reset(t)
    else:
        t.write8(REG_SYS_CFG, reg & ~SW_OFFLOAD_EN)


def iol_execute(t, control: int) -> bool:
    """Trigger an MCU command and wait for it to clear. [SRC] iol_execute.

    Writes ``control`` into REG_HMEBOX_E0, polls until those bits clear, then reads
    once more for the status (the command bit clear AND its <<4 error bit clear)."""
    control &= 0x0F
    reg = t.read8(REG_HMEBOX_E0)
    t.write8(REG_HMEBOX_E0, reg | control)
    for _ in range(_IOL_POLL_CAP):
        reg = t.read8(REG_HMEBOX_E0)
        if not (reg & control):
            break
    reg = t.read8(REG_HMEBOX_E0)                     # final status read
    return not (reg & control) and not (reg & (control << 4))


def iol_efuse_patch(t) -> bool:
    """``rtl8188e_iol_efuse_patch`` (HAL_INIT_STAGES_EFUSE_PATCH) [SRC]
    rtl8188e_hal_init.c:422 — read the efuse map into the MCU, then apply patches."""
    iol_mode_enable(t, True)
    ok = iol_execute(t, CMD_READ_EFUSE_MAP)
    if ok:
        ok = iol_execute(t, CMD_EFUSE_PATCH)
    iol_mode_enable(t, False)
    return ok


# --- probe-phase efuse read: map readback + PG decode ---------------------
def _read_phymap_from_txpktbuf(t, bcnhead: int = 0) -> bytes:
    """``efuse_read_phymap_from_txpktbuf`` [SRC] rtl8188e_hal_init.c:253 — read the
    MCU-loaded efuse physical map out of the TX packet buffer over the PKTBUF debug
    port. The first word is the byte length; the rest is the physical map."""
    t.write8(REG_PKT_BUFF_ACCESS_CTRL, TXPKT_BUF_SELECT)
    dbg_addr = bcnhead * 128 // 8
    out = bytearray()
    limit = EFUSE_MAP_LEN_88E
    length = EFUSE_MAP_LEN_88E
    count = 0
    i = 0
    while True:
        t.write16(REG_PKTBUF_DBG_ADDR, dbg_addr + i)
        t.write8(REG_TXPKTBUF_DBG, 0)
        for _ in range(_IOL_POLL_CAP):
            if t.read8(REG_TXPKTBUF_DBG):
                break
        lo = t.read32(REG_PKTBUF_DBG_DATA_L)
        hi = t.read32(REG_PKTBUF_DBG_DATA_H)
        lo_b = lo.to_bytes(4, "little")
        hi_b = hi.to_bytes(4, "little")
        if i == 0:
            # Vendor's compiled-in debug block re-reads the length word byte-wise.
            t.read8(REG_PKTBUF_DBG_DATA_L)
            t.read8(REG_PKTBUF_DBG_DATA_L + 1)
            length = lo & 0xFFFF                         # first word = total length
            limit = min(length - 2, limit)
            n = 2 if limit >= count + 2 else limit - count
            out += lo_b[2:2 + n]                         # the 2 bytes after the length
            count += n
        else:
            n = 4 if limit >= count + 4 else limit - count
            out += lo_b[:n]
            count += n
        if limit > count and length - 2 > count:
            n = 4 if limit >= count + 4 else limit - count
            out += hi_b[:n]
            count += n
        if limit <= count or length - 2 <= count:
            break
        i += 1
    t.write8(REG_PKT_BUFF_ACCESS_CTRL, DISABLE_TRXPKT_BUF_ACCESS)
    return bytes(out)


def _phymap_to_logical(phymap: bytes) -> bytes:
    """``efuse_phymap_to_logical`` [SRC] rtl8188e_hal_init.c:97 — walk PG headers
    (section offset + 4-bit word-enable; EXT_HEADER for offsets >= 16) into the
    512-byte logical map (`section*8 + word*2`)."""
    table = bytearray(b"\xFF" * EFUSE_MAP_LEN_88E)
    words = [[0xFFFF] * EFUSE_MAX_WORD_UNIT for _ in range(EFUSE_MAX_SECTION_88E)]

    def rd(addr):
        return phymap[addr] if addr < len(phymap) else 0xFF

    addr = 0
    if rd(addr) == 0xFF:
        return bytes(table)
    addr += 1
    cur = rd(0)
    while cur != 0xFF and addr < EFUSE_REAL_CONTENT_LEN_88E:
        if (cur & 0x1F) == 0x0F:                         # extended header
            u1 = (cur & 0xE0) >> 5
            cur = rd(addr)
            if (cur & 0x0F) == 0x0F:                     # invalid -> skip
                addr += 1
                cur = rd(addr)
                if cur != 0xFF and addr < EFUSE_REAL_CONTENT_LEN_88E:
                    addr += 1
                continue
            offset = ((cur & 0xF0) >> 1) | u1
            wren = cur & 0x0F
            addr += 1
        else:
            offset = (cur >> 4) & 0x0F
            wren = cur & 0x0F
        if offset < EFUSE_MAX_SECTION_88E:
            for w in range(EFUSE_MAX_WORD_UNIT):
                if not (wren & 0x01):
                    words[offset][w] = rd(addr)
                    addr += 1
                    if addr >= EFUSE_REAL_CONTENT_LEN_88E:
                        break
                    words[offset][w] |= rd(addr) << 8
                    addr += 1
                    if addr >= EFUSE_REAL_CONTENT_LEN_88E:
                        break
                wren >>= 1
        else:                                            # bad offset: skip its data
            for w in range(EFUSE_MAX_WORD_UNIT):
                if not (wren & 0x01):
                    addr += 2
                wren >>= 1
        cur = rd(addr)
        if cur != 0xFF and addr < EFUSE_REAL_CONTENT_LEN_88E:
            addr += 1

    for s in range(EFUSE_MAX_SECTION_88E):
        for w in range(EFUSE_MAX_WORD_UNIT):
            table[s * 8 + w * 2] = words[s][w] & 0xFF
            table[s * 8 + w * 2 + 1] = (words[s][w] >> 8) & 0xFF
    return bytes(table)


# 0xCA[3:2] PA/LNA select -> (ExternalPA_2G, ExternalLNA_2G) [SRC] Hal_ReadPAType_8188E
# rtl8188e_hal_init.c:3060 (ePA+eLNA=0, ePA+iLNA=1, iPA+eLNA=2, iPA+iLNA=3).
_PA_LNA_2G = {0: (True, True), 1: (True, False), 2: (False, True), 3: (False, False)}
# GLNA gain-select 0xCA[6:4] -> TypeGLNA [SRC] Hal_ReadAmplifierType_8188E:3146 (0->0x1
# 10dB, 2->0x2 14dB, others->0x0 unsupported).
_GLNA_TYPE = {0: 0x1, 2: 0x2}


def read_board_options(m: bytes) -> BoardOptions:
    """Decode the external PA/LNA board options from the logical efuse map, AUTO path
    (the registry amplifier/GLNA-type overrides default to 0 / AUTO). [SRC]
    Hal_ReadPAType_8188E rtl8188e_hal_init.c:3044 + Hal_ReadAmplifierType_8188E:3122.

    Wire effect: ExternalPA_2G/ExternalLNA_2G set the phydm board_type (GPA/GLNA) and
    type_glna words that gate board-conditional rows in the MAC/PHY_REG/AGC/RADIO_A
    init tables [SRC] hal_dm.c:224-260, and arm PHY_SetRFEReg_8188E (bb.phy_set_rfe_reg).
    A blank 0xCA (0xFF, this dev card) decodes to internal PA+LNA (bits[3:2]=3) with
    TypeGLNA 0 -> the reference walk. Of the other 0xCA/board bytes none reaches the
    wire in this build: ``CONFIG_ANTENNA_DIVERSITY`` off (0xC9 [SRC] autoconf.h:94),
    ``CONFIG_TXPWR_LIMIT_EN`` off (0xC1 [SRC] Makefile), 0xB8 only picks the SW channel
    list, and rfe_type (0xCA[1:0]) never gates a table row nor changes PHY_SetRFEReg's
    single case-0 arm."""
    rfe = m[EEPROM_RFE_OPTION_88E]
    ext_pa, ext_lna = _PA_LNA_2G[(rfe >> 2) & 0x3]         # 0xCA[3:2]
    type_glna = _GLNA_TYPE.get((rfe >> 4) & 0x7, 0x0)      # 0xCA[6:4]
    return BoardOptions(external_pa_2g=ext_pa, external_lna_2g=ext_lna,
                        type_glna=type_glna)


def _s4(n: int) -> int:
    """Signed 4-bit nibble -> int (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return n - 16 if (n & 0x8) else n


def _parse_tx_power(m: bytes) -> TxPwr2G:
    """``hal_load_pg_txpwr_info_path_2g`` (path A, 1T1R) [SRC] hal_com_phycfg.c:576 —
    the 18-byte 2.4G PG block at pg_txpwr_saddr: 6 CCK base groups, 5 BW40 base groups,
    then the diff bytes. The 1TX diff byte packs MSB=BW20, LSB=OFDM; the CCK 1TX diff
    has no efuse byte (CCK base is the 1TX reference) so it stays 0."""
    base = EEPROM_TX_PWR_INX_88E
    cck_base = tuple(m[base + i] for i in range(6))
    bw40_base = tuple(m[base + 6 + i] for i in range(5))
    diff = m[base + 11]                              # tx_idx 0 diff byte
    return TxPwr2G(cck_base, bw40_base, 0, _s4(diff & 0x0F), _s4(diff >> 4))


def read_chip_params(t, bcnhead: int = 0) -> ChipParams:
    """Probe-phase efuse read [SRC] ReadEFuseByIC -> iol_read_efuse: power-on (done
    by the caller) then IOL READ_EFUSE_MAP, read the physical map out of the packet
    buffer, decode it, and extract crystal_cap + MAC. Returns the logical map too."""
    iol_mode_enable(t, True, fw_ready=False)
    t.write8(REG_TDECTRL + 1, bcnhead)                   # iol_read_efuse: bndy
    t.write8(REG_PKT_BUFF_ACCESS_CTRL, TXPKT_BUF_SELECT)
    iol_execute(t, CMD_READ_EFUSE_MAP)
    phymap = _read_phymap_from_txpktbuf(t, bcnhead)
    iol_mode_enable(t, False)
    # hal_EfusePowerSwitch(OFF) tail: disable efuse access (bWrite=FALSE -> just 0xCF).
    t.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)

    logical = _phymap_to_logical(phymap)
    cap = logical[EEPROM_XTAL_88E]
    if cap == 0xFF:
        cap = DEFAULT_CRYSTAL_CAP
    mac = logical[EEPROM_MAC_ADDR_88EU:EEPROM_MAC_ADDR_88EU + 6]
    return ChipParams(crystal_cap=cap, mac_address=mac, efuse_map=logical,
                      tx_power=_parse_tx_power(logical),
                      board=read_board_options(logical))
