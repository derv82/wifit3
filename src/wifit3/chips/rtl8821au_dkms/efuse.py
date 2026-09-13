"""RTL8821AU EFUSE read + chip-param decode — vendor port.

The probe phase reads the burned-in efuse to recover per-card parameters: the
``crystal_cap`` (AFE trim, replaces the M3 hardcode), the ``mac_address``, and the
per-rate-group 2.4 GHz TX-power base + nTX diffs that feed the M-TXPWR txagc sweep.
Vendor path: ReadAdapterInfo8812AU -> hal_ReadPROMContent_8812A ->
Hal_EfuseParseTxPowerInfo_8812A, over efuse_ReadEFuse + ReadEFuseByte.

Mechanism ([SRC] ReadEFuseByte rtw_efuse.c:2209 + the PG-block format; [WIRE] cap1
frames 65..2475, device 39):

  * Physical read: each byte writes the 10-bit address (EFUSE_CTRL+1 / +2), clears
    EFUSE_CTRL+3 bit7 to trigger, polls EFUSE_CTRL bit31 for ready, then takes the low
    byte of the 32-bit EFUSE_CTRL.
  * Header unpacking: the physical efuse is a stream of PG blocks (header = section
    offset + 4-bit word-enable, or an EXT_HEADER); enabled words flatten into a 512 B
    logical map (``section*8 + word*2``). Identical PG format to the rtl8814au_dkms
    sibling.
  * Parse: crystal_cap / mac / TX-power read at fixed logical offsets (path A, 1T1R).

Gated by REG_EFUSE_ACCESS (0x69 on, 0x00 off). The 8821au is 1T1R, so only path A's
PG TX-power block (starting at pg_txpwr_saddr=0x10) is decoded.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from . import constants as C
from .phy_cond import JaguarParams

# ODM board_type bitfield [SRC] hal_dm.c:382-405 — the GLNA/GPA/ALNA/APA/BT bits the
# JaguarSeries phy_cond walker matches against.
ODM_BOARD_BT = 1 << 2
ODM_BOARD_EXT_PA_2G = 1 << 3      # ODM_BOARD_EXT_PA  (2G PA)
ODM_BOARD_EXT_LNA_2G = 1 << 4     # ODM_BOARD_EXT_LNA (2G LNA)
ODM_BOARD_EXT_PA_5G = 1 << 6
ODM_BOARD_EXT_LNA_5G = 1 << 7


class PathTxPwr(NamedTuple):
    """Per-RF-path 2.4 GHz TX-power base + nTX diffs (efuse PG data)."""
    cck_base: tuple    # 6 channel groups
    bw40_base: tuple   # 5 channel groups (also the OFDM/HT/VHT base)
    cck_diff: tuple    # [1TX, 2TX, 3TX]  (1TX has no efuse byte -> 0)
    ofdm_diff: tuple
    bw20_diff: tuple


class ChipParams(NamedTuple):
    crystal_cap: int
    mac_address: Optional[str]
    chip_version: int
    autoload_fail: bool
    eeprom_id_valid: bool
    eeprom_version: int
    eeprom_vid: int
    eeprom_pid: int
    eeprom_customer_id: int
    eeprom_subcustomer_id: int
    customer_id: int
    regulatory: int
    interface_sel: int
    channel_plan: int
    country_code: str
    thermal_meter: int
    thermal_meter_ignore: bool
    remote_wakeup: bool
    usb_mode_switch: bool
    usb_type_antenna: int
    usb_type_wmode: int
    usb_type_disable_11ac: bool
    pa_type_2g: int
    lna_type_2g: int
    pa_type_5g: int
    lna_type_5g: int
    external_pa_2g: bool
    external_lna_2g: bool
    external_pa_5g: bool
    external_lna_5g: bool
    bt_coexist: bool         # EEPROMBluetoothCoexist — ODM_BOARD_BT policy bit
    bt_ant_num: int
    tx_power: PathTxPwr      # path A, 2.4 GHz
    tx_power_5g: PathTxPwr   # path A, 5 GHz (bw40_base = 14 UNII groups; no CCK)
    bb_swing_2g: int         # path-A TxScale (0x0C1C[31:21]) for 2.4 GHz
    bb_swing_5g: int         # path-A TxScale for 5 GHz
    ext_lna_2g: bool         # ExternalLNA_2G — gates the phy_SetRFEReg8821 2.4 GHz pinmux
    board_type: int          # ODM ext-LNA/PA/BT bitfield — the phy_cond walker's board input


def _efuse_one_byte_read(t, addr: int) -> int:
    """[SRC] ReadEFuseByte (rtw_efuse.c:2209) — one physical efuse byte.

    Write the 10-bit address (EFUSE_CTRL+1 low, +2 high preserving the top 6 bits),
    clear EFUSE_CTRL+3 bit7 to trigger, poll EFUSE_CTRL bit31 (ready), take the low byte.
    """
    t.write8(C.REG_EFUSE_CTRL + 1, addr & 0xFF)
    v = t.read8(C.REG_EFUSE_CTRL + 2)
    t.write8(C.REG_EFUSE_CTRL + 2, ((addr >> 8) & 0x03) | (v & 0xFC))
    v = t.read8(C.REG_EFUSE_CTRL + 3)
    t.write8(C.REG_EFUSE_CTRL + 3, v & 0x7F)
    value32 = t.read32(C.REG_EFUSE_CTRL)
    retry = 0
    while not ((value32 >> 24) & 0x80) and retry < 10000:
        value32 = t.read32(C.REG_EFUSE_CTRL)
        retry += 1
    value32 = t.read32(C.REG_EFUSE_CTRL)   # re-read after the HW settle delay
    return value32 & 0xFF


def _read_logical_map(t) -> bytes:
    """[SRC] efuse_ReadEFuse — physical efuse PG stream -> 512 B logical map.

    Same PG-block format as the rtl8814au_dkms sibling: each block has a header
    (section offset + 4-bit word-enable, or an EXT_HEADER form) and contributes two
    bytes per enabled word to eFuseWord[section][word]; the 64x4 words flatten into the
    logical map at ``section*8 + word*2``.
    """
    word = [[0xFFFF] * C.EFUSE_MAX_WORD_UNIT for _ in range(C.EFUSE_MAX_SECTION_JAGUAR)]
    addr = 0

    header = _efuse_one_byte_read(t, addr)
    addr += 1
    if header == 0xFF:
        return b"\xFF" * C.EFUSE_MAP_LEN_JAGUAR          # empty efuse

    while header != 0xFF and addr < C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
        if (header & 0x1F) == 0x0F:               # EXT_HEADER
            offset_2_0 = (header & 0xE0) >> 5
            ext = _efuse_one_byte_read(t, addr)
            addr += 1
            if ext == 0xFF:
                break
            if (ext & 0x0F) == 0x0F:              # ALL_WORDS_DISABLED
                header = _efuse_one_byte_read(t, addr)
                addr += 1
                continue
            offset = ((ext & 0xF0) >> 1) | offset_2_0
            wden = ext & 0x0F
        else:
            offset = (header >> 4) & 0x0F
            wden = header & 0x0F

        if offset < C.EFUSE_MAX_SECTION_JAGUAR:
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):               # word disabled
                    continue
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] = data & 0xFF
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] |= (data << 8) & 0xFF00
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
        else:                                     # invalid offset — skip its words
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):
                    continue
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN_JAGUAR:
                    break

        header = _efuse_one_byte_read(t, addr)
        if header != 0xFF:
            addr += 1

    tbl = bytearray(b"\xFF" * C.EFUSE_MAP_LEN_JAGUAR)
    for i in range(C.EFUSE_MAX_SECTION_JAGUAR):
        for j in range(C.EFUSE_MAX_WORD_UNIT):
            tbl[i * 8 + j * 2] = word[i][j] & 0xFF
            tbl[i * 8 + j * 2 + 1] = (word[i][j] >> 8) & 0xFF
    return bytes(tbl)


def _le16(m: bytes, off: int) -> int:
    return m[off] | (m[off + 1] << 8)


def _eeprom_id_valid(m: bytes) -> bool:
    """[SRC] Hal_EfuseParseIDCode8812A — logical bytes 0..1 must be 0x8129."""
    return _le16(m, 0) == C.RTL_EEPROM_ID


def _parse_crystal_cap(m: bytes, autoload_fail: bool) -> int:
    """[SRC] Hal_EfuseParseXtal_8812A — efuse 0xB9, else default 0x20."""
    if autoload_fail:
        return C.EEPROM_DEFAULT_CRYSTAL_CAP
    v = m[C.EEPROM_XTAL]
    return C.EEPROM_DEFAULT_CRYSTAL_CAP if v == 0xFF else v


def _parse_eeprom_version(m: bytes, autoload_fail: bool) -> int:
    """[SRC] Hal_ReadPROMVersion8812A — 8821 version byte 0xC4."""
    if autoload_fail:
        return C.EEPROM_DEFAULT_VERSION
    v = m[C.EEPROM_VERSION_8821]
    return C.EEPROM_DEFAULT_VERSION if v == 0xFF else v


def _customer_id_from_ids(vid: int, pid: int) -> int:
    if (vid, pid) == (0x050D, 0x1106):
        return C.RT_CID_819X_SERCOMM_BELKIN
    if (vid, pid) == (0x0846, 0x9051):
        return C.RT_CID_819X_SERCOMM_NETGEAR
    if (vid, pid) == (0x2001, 0x330E):
        return C.RT_CID_819X_ALPHA_DLINK
    if (vid, pid) == (0x0B05, 0x17D2):
        return C.RT_CID_819X_EDIMAX_ASUS
    if (vid, pid) == (0x0846, 0x9052):
        return C.RT_CID_NETGEAR
    if vid == 0x0411 and pid in (0x0242, 0x025D):
        return C.RT_CID_DNI_BUFFALO
    if (vid, pid) in ((0x2001, 0x3314), (0x20F4, 0x804B), (0x20F4, 0x805B),
                      (0x2001, 0x3315), (0x2001, 0x3316)):
        return C.RT_CID_DLINK
    return C.RT_CID_DEFAULT


def _customized_customer_id(vid: int, pid: int, eeprom_customer_id: int, base_id: int) -> int:
    """[SRC] hal_CustomizeByCustomerID_8812AU — resolved CustomerID for LED/customer policy."""
    resolved = base_id
    if (vid, pid) == (0x103C, 0x1629):
        resolved = C.RT_CID_819X_HP
    elif (vid, pid) == (0x9846, 0x9041):
        resolved = C.RT_CID_NETGEAR
    elif (vid, pid) == (0x2019, 0x1201):
        resolved = C.RT_CID_PLANEX
    elif (vid, pid) == (0x0BDA, 0x5088):
        resolved = C.RT_CID_CC_C
    elif vid == 0x0411 and pid in (0x0242, 0x025D):
        resolved = C.RT_CID_DNI_BUFFALO
    elif (vid, pid) in ((0x2001, 0x3314), (0x20F4, 0x804B), (0x20F4, 0x805B),
                        (0x2001, 0x3315), (0x2001, 0x3316)):
        resolved = C.RT_CID_DLINK

    if eeprom_customer_id == C.EEPROM_CID_DEFAULT:
        if vid == 0x2001 and pid in (0x3308, 0x3309, 0x330A):
            return C.RT_CID_DLINK
        if (vid, pid) == (0x0BFF, 0x8160):
            return C.RT_CID_CHINA_MOBILE
        if (vid, pid) == (0x0BDA, 0x5088):
            return C.RT_CID_CC_C
        if (vid, pid) == (0x0846, 0x9052):
            return C.RT_CID_NETGEAR
        if vid == 0x0411 and pid in (0x0242, 0x025D):
            return C.RT_CID_DNI_BUFFALO
        if (vid, pid) in ((0x2001, 0x3314), (0x20F4, 0x804B), (0x20F4, 0x805B),
                          (0x2001, 0x3315), (0x2001, 0x3316)):
            return C.RT_CID_DLINK
        return resolved
    if eeprom_customer_id == C.EEPROM_CID_WHQL:
        return resolved
    return C.RT_CID_DEFAULT


def _parse_ids(m: bytes, autoload_fail: bool) -> tuple[int, int, int, int, int]:
    """[SRC] hal_ReadIDs_8812AU + hal_CustomizeByCustomerID_8812AU."""
    if autoload_fail:
        return (C.EEPROM_DEFAULT_VID, C.EEPROM_DEFAULT_PID, C.EEPROM_DEFAULT_CUSTOMER_ID,
                C.EEPROM_DEFAULT_SUBCUSTOMER_ID, C.RT_CID_DEFAULT)
    vid = _le16(m, C.EEPROM_VID_8821AU)
    pid = _le16(m, C.EEPROM_PID_8821AU)
    customer = m[C.EEPROM_CUSTOM_ID_8812]
    subcustomer = C.EEPROM_DEFAULT_SUBCUSTOMER_ID
    return vid, pid, customer, subcustomer, _customized_customer_id(
        vid, pid, customer, _customer_id_from_ids(vid, pid))


def _parse_regulatory(m: bytes, autoload_fail: bool) -> int:
    """[SRC] Hal_ReadTxPowerInfo8812A — board-option bits [2:0]."""
    if autoload_fail:
        return 0
    board = m[C.EEPROM_RF_BOARD_OPTION_8821AU]
    if board == 0xFF:
        board = C.EEPROM_DEFAULT_BOARD_OPTION
    return board & 0x7


def _parse_interface_sel(m: bytes, autoload_fail: bool) -> int:
    """[SRC] Hal_ReadBoardType8812A — board-option bits [7:5]."""
    if autoload_fail:
        return 0
    board = m[C.EEPROM_RF_BOARD_OPTION_8821AU]
    if board == 0xFF:
        board = C.EEPROM_DEFAULT_BOARD_OPTION
    return (board & 0xE0) >> 5


def _parse_country_code(m: bytes) -> str:
    raw = m[C.EEPROM_COUNTRY_CODE_8812:C.EEPROM_COUNTRY_CODE_8812 + 2]
    if len(raw) != 2 or raw in (b"\xff\xff", b"\x00\x00"):
        return ""
    return raw.decode("ascii", errors="replace")


def _parse_thermal_meter(m: bytes, autoload_fail: bool) -> tuple[int, bool]:
    """[SRC] Hal_ReadThermalMeter_8812A — blank/fail means ignore and report 0xFF."""
    v = C.EEPROM_DEFAULT_THERMAL_METER_8812 if autoload_fail else m[C.EEPROM_THERMAL_METER_8821]
    if v == 0xFF or autoload_fail:
        return 0xFF, True
    return v, False


def _s4(n: int) -> int:
    """Signed 4-bit nibble -> int (PG_TXPWR_*_DIFF_TO_S8BIT)."""
    return n - 16 if (n & 0x8) else n


def _parse_tx_power(m: bytes) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_2g — path-A base + nTX diff nibbles.

    The path-A 2.4 GHz PG block is 18 B at pg_txpwr_saddr (0x10): 6 CCK group bases, 5
    BW40 group bases, then 7 diff bytes packing signed nibbles —
      11: MSB=BW20[1TX] LSB=OFDM[1TX]   12: MSB=BW40[2TX] LSB=BW20[2TX]
      13: MSB=OFDM[2TX] LSB=CCK[2TX]    14: MSB=BW40[3TX] LSB=BW20[3TX]
      15: MSB=OFDM[3TX] LSB=CCK[3TX]    16,17: 4TX
    CCK[1TX] has no byte (CCK base is the 1TX reference) -> 0. The 8821au is 1T1R, so
    only the 1TX diffs are ever applied (phy_get_pg_txpwr_idx with ntx_idx==1).
    """
    base = C.PG_TXPWR_SADDR
    cck_base = tuple(m[base + i] for i in range(6))
    bw40_base = tuple(m[base + 6 + i] for i in range(5))
    d = [m[base + 11 + i] for i in range(7)]
    bw20_diff = (_s4(d[0] >> 4), _s4(d[1] & 0xF), _s4(d[3] & 0xF))
    ofdm_diff = (_s4(d[0] & 0xF), _s4(d[2] >> 4), _s4(d[4] >> 4))
    cck_diff = (0, _s4(d[2] & 0xF), _s4(d[4] & 0xF))
    return PathTxPwr(cck_base, bw40_base, cck_diff, ofdm_diff, bw20_diff)


def _parse_tx_power_5g(m: bytes) -> PathTxPwr:
    """[SRC] hal_load_pg_txpwr_info_path_5g — path-A 5 GHz base + nTX diff nibbles.

    The 24 B 5 GHz PG block follows the 18 B 2.4G block (so it starts at 0x22 for path A):
    14 BW40 group bases (the 14 UNII groups), then the diff bytes —
      14: MSB=BW20[1T] LSB=OFDM[1T]   15: MSB=BW40[2T] LSB=BW20[2T]
      16: MSB=BW40[3T] LSB=BW20[3T]   18: MSB=OFDM[2T] LSB=OFDM[3T]
    No CCK on 5 GHz. For 20 MHz only the BW40 base + OFDM/BW20 1TX diffs are used.
    """
    b5 = C.PG_TXPWR_SADDR + 18
    bw40_base = tuple(m[b5 + i] for i in range(14))
    b14, b18 = m[b5 + 14], m[b5 + 18]
    ofdm_diff = (_s4(b14 & 0xF), _s4(b18 >> 4), _s4(b18 & 0xF))   # 1T, 2T, 3T
    bw20_diff = (_s4(b14 >> 4), _s4(m[b5 + 15] & 0xF), _s4(m[b5 + 16] & 0xF))
    return PathTxPwr((), bw40_base, (), ofdm_diff, bw20_diff)


# Per-path BB-swing: a 2-bit index per path maps to an 11-bit TxScale value, band-
# independent (only the efuse byte differs: 0xC6 = 2.4 GHz, 0xC7 = 5 GHz). [SRC]
# PHY_GetTxBBSwing_8812A: 0->0 dB, 1->-3 dB, 2->-6 dB, 3->-9 dB.
_BB_SWING = {0: 0x200, 1: 0x16A, 2: 0x101, 3: 0x0B6}


def _parse_bb_swing(m: bytes, byte_off: int) -> int:
    """[SRC] PHY_GetTxBBSwing_8812A (registry AUTO) — path-A TxScale for one band.

    The efuse byte packs a 2-bit swing index per path (A = bits[1:0]); an unburned byte
    (0xFF) means 0 dB. The 8821au is 1T1R, so only path A is read.
    """
    sw = m[byte_off]
    if sw == 0xFF:
        sw = 0x00
    return _BB_SWING[sw & 0x3]


def _ext_amplifier_flags(m: bytes, autoload_fail: bool) -> tuple[int, int, int, int, bool, bool, bool, bool]:
    """[SRC] Hal_ReadPAType_8821A — raw PA/LNA type bytes and the four external flags."""
    if autoload_fail:
        return 0, 0, 0, 0, False, False, False, False
    pa = m[C.EEPROM_PA_TYPE_8821AU]
    lna_2g = m[C.EEPROM_LNA_TYPE_2G_8821AU]
    lna_5g = m[C.EEPROM_LNA_TYPE_5G_8821AU]
    if pa == 0xFF:
        pa = 0
    if lna_2g == 0xFF:
        lna_2g = 0
    if lna_5g == 0xFF:
        lna_5g = 0
    ext_pa_2g = bool(pa & C.BIT(4))
    ext_lna_2g = bool(lna_2g & C.BIT(3))
    ext_pa_5g = bool(pa & C.BIT0)
    ext_lna_5g = bool(lna_5g & C.BIT(3))
    return pa, lna_2g, pa, lna_5g, ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g


def _parse_bt_coexist(m: bytes, multi_func_ctrl: int, autoload_fail: bool) -> tuple[bool, int]:
    """[SRC] Hal_EfuseParseBTCoexistInfo8812A — 8821U uses REG_MULTI_FUNC_CTRL only."""
    if autoload_fail:
        return False, 1
    return bool(multi_func_ctrl & C.BIT_BT_FUNC_EN), m[C.EEPROM_RF_BT_SETTING_8821] & 0x1


def _parse_board_type(ext: tuple, bt_coexist: bool = False) -> int:
    """[SRC] hal_dm.c:382-405 — assemble the ODM board_type from EFUSE policy flags."""
    ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g = ext[4:]
    board_type = ODM_BOARD_BT if bt_coexist else 0
    if ext_lna_2g:
        board_type |= ODM_BOARD_EXT_LNA_2G
    if ext_lna_5g:
        board_type |= ODM_BOARD_EXT_LNA_5G
    if ext_pa_2g:
        board_type |= ODM_BOARD_EXT_PA_2G
    if ext_pa_5g:
        board_type |= ODM_BOARD_EXT_PA_5G
    return board_type


def _parse_remote_wakeup(m: bytes, autoload_fail: bool) -> bool:
    """[SRC] Hal_ReadRemoteWakeup_8812A — 8821U optional function byte 0x104 bit1."""
    return False if autoload_fail else bool(m[C.EEPROM_USB_OPTIONAL_FUNCTION0_8811AU] & C.BIT1)


def _parse_usb_mode_switch(m: bytes, autoload_fail: bool) -> bool:
    """[SRC] hal_ReadUsbModeSwitch_8812AU — EEPROM_USB_MODE_8812 bit1."""
    return False if autoload_fail else bool((m[C.EEPROM_USB_MODE_8812] & C.BIT1) >> 1)


def _parse_usb_type_hidden(phy: dict[int, int], m: bytes) -> tuple[int, int, bool]:
    """[SRC] hal_ReadUsbType_8812AU — hidden physical EFUSE antenna/wmode selector."""
    antenna = 0
    for addr in (C.EFUSE_HIDDEN_USB_TYPE_ANTENNA_0, C.EFUSE_HIDDEN_USB_TYPE_ANTENNA_1):
        v = phy.get(addr, 0xFF)
        if ((v >> 5) & 0x7) != 0:
            antenna = (v >> 5) & 0x7
            break
        if ((v >> 1) & 0x7) != 0:
            antenna = (v >> 1) & 0x7
            break
    wmode = 0
    for addr in (C.EFUSE_HIDDEN_USB_TYPE_WMODE_0, C.EFUSE_HIDDEN_USB_TYPE_WMODE_1):
        v = phy.get(addr, 0xFF)
        if ((v >> 2) & 0x3) != 0:
            wmode = (v >> 2) & 0x3
            break
    disable_11ac = antenna == 2 and wmode == 2
    if antenna == 2 and wmode == 3 and m[C.EEPROM_USB_MODE_8812] == 0x2:
        disable_11ac = False
    return antenna, wmode, disable_11ac


def _parse_mac_address(m: bytes, autoload_fail: bool) -> Optional[str]:
    """[SRC] hal_config_macaddr — ignore hardware PG MAC when autoload/ID validation fails."""
    if autoload_fail:
        return None
    mac = m[C.EEPROM_MAC_ADDR_8821AU:C.EEPROM_MAC_ADDR_8821AU + 6]
    if len(mac) != 6 or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + efuse read. [WIRE] cap1 frames 65..2475.

    Reproduces the capture's pre-power-on sequence: chip-version read, autoload check,
    EFUSE access-on + power-switch reads, the byte loop, access-off.
    """
    chip_version = t.read32(C.REG_SYS_CFG)         # ReadChipVersion (0xF0)
    multi_func_ctrl = t.read32(C.REG_MULTI_FUNC_CTRL)  # Hal_EfuseParseBTCoexistInfo input
    ee = t.read8(C.REG_9346CR)
    autoload_fail = not (ee & (1 << 5))            # bit5 = EEPROM present

    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ON)
    t.read16(0x0000)                               # EFUSE power-switch status reads
    t.read16(0x0002)
    t.read16(0x0008)
    m = _read_logical_map(t)
    hidden_phy = {
        addr: _efuse_one_byte_read(t, addr)
        for addr in (C.EFUSE_HIDDEN_USB_TYPE_ANTENNA_0, C.EFUSE_HIDDEN_USB_TYPE_ANTENNA_1,
                     C.EFUSE_HIDDEN_USB_TYPE_WMODE_0, C.EFUSE_HIDDEN_USB_TYPE_WMODE_1)
    }
    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_OFF)

    eeprom_id_valid = _eeprom_id_valid(m)           # Hal_EfuseParseIDCode8812A
    autoload_fail = not eeprom_id_valid
    ext = _ext_amplifier_flags(m, autoload_fail)   # Hal_ReadPAType_8821A
    bt_coexist, bt_ant_num = _parse_bt_coexist(m, multi_func_ctrl, autoload_fail)
    eeprom_vid, eeprom_pid, eeprom_customer_id, eeprom_subcustomer_id, customer_id = _parse_ids(
        m, autoload_fail)
    thermal_meter, thermal_meter_ignore = _parse_thermal_meter(m, autoload_fail)
    usb_type_antenna, usb_type_wmode, usb_type_disable_11ac = _parse_usb_type_hidden(hidden_phy, m)
    return ChipParams(
        crystal_cap=_parse_crystal_cap(m, autoload_fail),
        mac_address=_parse_mac_address(m, autoload_fail),
        chip_version=chip_version,
        autoload_fail=autoload_fail,
        eeprom_id_valid=eeprom_id_valid,
        eeprom_version=_parse_eeprom_version(m, autoload_fail),
        eeprom_vid=eeprom_vid,
        eeprom_pid=eeprom_pid,
        eeprom_customer_id=eeprom_customer_id,
        eeprom_subcustomer_id=eeprom_subcustomer_id,
        customer_id=customer_id,
        regulatory=_parse_regulatory(m, autoload_fail),
        interface_sel=_parse_interface_sel(m, autoload_fail),
        channel_plan=m[C.EEPROM_CHANNEL_PLAN_8821],
        country_code=_parse_country_code(m),
        thermal_meter=thermal_meter,
        thermal_meter_ignore=thermal_meter_ignore,
        remote_wakeup=_parse_remote_wakeup(m, autoload_fail),
        usb_mode_switch=_parse_usb_mode_switch(m, autoload_fail),
        usb_type_antenna=usb_type_antenna,
        usb_type_wmode=usb_type_wmode,
        usb_type_disable_11ac=usb_type_disable_11ac,
        pa_type_2g=ext[0],
        lna_type_2g=ext[1],
        pa_type_5g=ext[2],
        lna_type_5g=ext[3],
        external_pa_2g=ext[4],
        external_lna_2g=ext[5],
        external_pa_5g=ext[6],
        external_lna_5g=ext[7],
        bt_coexist=bt_coexist,
        bt_ant_num=bt_ant_num,
        tx_power=_parse_tx_power(m),
        tx_power_5g=_parse_tx_power_5g(m),
        bb_swing_2g=_parse_bb_swing(m, C.EEPROM_TX_BBSWING_2G),
        bb_swing_5g=_parse_bb_swing(m, C.EEPROM_TX_BBSWING_5G),
        ext_lna_2g=ext[5],
        board_type=_parse_board_type(ext, bt_coexist),
    )


def build_jaguar_params(params: ChipParams) -> JaguarParams:
    """Thread the EFUSE-decoded board params into the phy_cond walker inputs.

    The 8821a JaguarSeries BB/AGC/RADIO tables branch on board_type (ext-LNA/PA) and the
    USB/CE interface (kept at the JaguarParams defaults). They carry NO cut-version- or
    package-gated rows (every condition header's cut[27:24] and QFN[15:12] fields read 0),
    so cut_version is left at its default 0 — it has no effect on any taken row. type_* stay
    0 (the 8821 hal never populates TypeGLNA/GPA/ALNA/APA). For the reference AWUS036ACS
    (board_type 0) this reproduces the hardcoded default exactly; an ext-PA/LNA card walks
    its own (ported-but-untested) board rows.
    """
    return JaguarParams(board_type=params.board_type)
