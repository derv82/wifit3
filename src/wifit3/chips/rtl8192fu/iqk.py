import time

from .phy import read_rfreg, write_rfreg


RF_A = 0
RF_B = 1

REG_GPIO_MUXCFG = 0x0040
REG_LEDCFG0 = 0x004C
REG_PAD_CTRL1 = 0x0064
REG_TXPAUSE = 0x0522
REG_BEACON_CTRL = 0x0550
REG_BEACON_CTRL_1 = 0x0551
REG_FPGA0_XA_HSSI_PARM1 = 0x0820
REG_FPGA0_XCD_RF_SW_CTRL = 0x0874
REG_FPGA0_XCD_RF_PARM = 0x087C
REG_FPGA0_ANALOG4 = 0x088C
REG_RFE_OPT = 0x0920
REG_DPDT_CTRL = 0x092C
REG_RFE_CTRL_ANTA_SRC = 0x0930
REG_RFE_CTRL_ANT_SRC1 = 0x0934
REG_RFE_CTRL_ANT_SRC2 = 0x0938
REG_RFE_CTRL_ANT_SRC3 = 0x093C
REG_RFE_PATH_SELECT = 0x0940
REG_RFE_BUFFER = 0x0944
REG_RFE_OPT62 = 0x0968
REG_CCK0_AFE_SETTING = 0x0A04
REG_CONFIG_ANT_A = 0x0B68
REG_CONFIG_ANT_B = 0x0B6C
REG_OFDM0_TRX_PATH_ENABLE = 0x0C04
REG_OFDM0_TR_MUX_PAR = 0x0C08
REG_OFDM0_XA_RX_IQ_IMBALANCE = 0x0C14
REG_OFDM0_XB_RX_IQ_IMBALANCE = 0x0C1C
REG_OFDM0_ENERGY_CCA_THRES = 0x0C4C
REG_OFDM0_XA_AGC_CORE1 = 0x0C50
REG_OFDM0_XB_AGC_CORE1 = 0x0C58
REG_OFDM0_AGC_RSSI_TABLE = 0x0C78
REG_OFDM0_XA_TX_IQ_IMBALANCE = 0x0C80
REG_OFDM0_XB_TX_IQ_IMBALANCE = 0x0C88
REG_OFDM0_XC_TX_AFE = 0x0C94
REG_OFDM0_XD_TX_AFE = 0x0C9C
REG_OFDM0_RX_IQ_EXT_ANTA = 0x0CA0
REG_RXIQB_EXT = 0x0CA8
REG_ANAPWR1 = 0x0D94
REG_NP_ANTA = 0x0E20
REG_TAP_UPD_97F = 0x0E24
REG_FPGA0_IQK = 0x0E28
REG_TX_IQK_TONE_A = 0x0E30
REG_RX_IQK_TONE_A = 0x0E34
REG_TX_IQK_PI_A = 0x0E38
REG_RX_IQK_PI_A = 0x0E3C
REG_TX_IQK = 0x0E40
REG_RX_IQK = 0x0E44
REG_IQK_AGC_PTS = 0x0E48
REG_IQK_AGC_RSP = 0x0E4C
REG_TX_IQK_TONE_B = 0x0E50
REG_RX_IQK_TONE_B = 0x0E54
REG_TX_IQK_PI_B = 0x0E58
REG_RX_IQK_PI_B = 0x0E5C
REG_TX_POWER_BEFORE_IQK_A = 0x0E94
REG_IQK_RPT_TXA = 0x0E98
REG_TX_POWER_AFTER_IQK_A = 0x0E9C
REG_RX_POWER_BEFORE_IQK_A_2 = 0x0EA4
REG_IQK_RPT_RXA = 0x0EA8
REG_RX_POWER_AFTER_IQK_A_2 = 0x0EAC
REG_TX_POWER_BEFORE_IQK_B = 0x0EB4
REG_IQK_RPT_TXB = 0x0EB8
REG_TX_POWER_AFTER_IQK_B = 0x0EBC
REG_RX_POWER_BEFORE_IQK_B_2 = 0x0EC4
REG_IQK_RPT_RXB = 0x0EC8
REG_RX_POWER_AFTER_IQK_B_2 = 0x0ECC
REG_RX_WAIT_CCA = 0x0E70
REG_SW_GPIO_SHARE_CTRL_0 = 0x1038
REG_SW_GPIO_SHARE_CTRL_1 = 0x103C

RF6052_REG_AC = 0x00
RF6052_REG_TXPA_G3 = 0x33
RF6052_REG_GAIN_P1 = 0x35
RF6052_REG_PAD_TXG = 0x56
RF6052_REG_TXMOD = 0x58
RF6052_REG_GAIN_CCA = 0xDF
RF6052_REG_WE_LUT = 0xEF

MAX_TOLERANCE = 5


def _write32_mask(transport, register: int, mask: int, value: int) -> None:
    shift = (mask & -mask).bit_length() - 1
    original = transport.read32(register)
    transport.write32(register, (original & ~mask) | ((value << shift) & mask))


def _write32_set(transport, register: int, bits: int) -> None:
    transport.write32(register, transport.read32(register) | bits)


def _write32_clear(transport, register: int, bits: int) -> None:
    transport.write32(register, transport.read32(register) & ~bits)


def _write8_clear(transport, register: int, bits: int) -> None:
    transport.write8(register, transport.read8(register) & ~bits)


def _write_rfreg_mask(transport, path: int, register: int, mask: int, value: int) -> None:
    shift = (mask & -mask).bit_length() - 1
    original = read_rfreg(transport, path, register)
    write_rfreg(transport, path, register, (original & ~mask) | ((value << shift) & mask))


def _wait_iqk_report(transport, register: int) -> None:
    time.sleep(0.015)
    ktime = 0
    while transport.read32(register) == 0 and ktime < 21:
        time.sleep(0.005)
        ktime += 5


def _rtl8192fu_iqk_path_a(transport, rfe_type: int) -> int:
    result = 0
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    transport.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)
    transport.write32(REG_ANAPWR1, 0x44FFBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x00400040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000100)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 4, 1)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    val32 = 0x30 if rfe_type in (7, 8, 9, 12) else 0xE9
    _write_rfreg_mask(transport, RF_A, RF6052_REG_PAD_TXG, 0x003FF, val32)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x18008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_TX_IQK_PI_A, 0x8214000F)
    transport.write32(REG_RX_IQK_PI_A, 0x28140000)
    transport.write32(REG_TX_IQK, 0x01007C00)
    transport.write32(REG_RX_IQK, 0x01004800)
    transport.write32(REG_IQK_AGC_RSP, 0x00E62911)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_TXA)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = transport.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = transport.read32(REG_TX_POWER_AFTER_IQK_A)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_WE_LUT, 1 << 4, 1)
    val32 = read_rfreg(transport, RF_A, RF6052_REG_TXMOD)
    rf_0x58_i = (val32 & 0xFC000) >> 14
    rf_0x58_q = (val32 & 0x003F0) >> 4
    for i in range(8):
        _write_rfreg_mask(transport, RF_A, RF6052_REG_TXPA_G3, 0x1C000, i)
        _write_rfreg_mask(transport, RF_A, RF6052_REG_TXPA_G3, 0x00FC0, rf_0x58_i)
        _write_rfreg_mask(transport, RF_A, RF6052_REG_TXPA_G3, 0x0003F, rf_0x58_q)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_AC, 1 << 14, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_WE_LUT, 1 << 4, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 0x00810, 0)
    if not reg_eac & (1 << 28) and reg_e94 & 0x03FF0000 != 0x01420000 and reg_e9c & 0x03FF0000 != 0x00420000:
        result |= 0x01
    return result


def _rtl8192fu_rx_iqk_path_a(transport) -> int:
    result = 0
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 1, 1)
    write_rfreg(transport, RF_A, RF6052_REG_GAIN_P1, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_PAD_TXG, 0x003FF, 0x27)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x18008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_TX_IQK_PI_A, 0x82160027)
    transport.write32(REG_RX_IQK_PI_A, 0x28160000)
    transport.write32(REG_TX_IQK, 0x01007C00)
    transport.write32(REG_RX_IQK, 0x01004800)
    transport.write32(REG_IQK_AGC_RSP, 0x0086A911)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_TXA)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = transport.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = transport.read32(REG_TX_POWER_AFTER_IQK_A)
    if not reg_eac & (1 << 28) and reg_e94 & 0x03FF0000 != 0x01420000 and reg_e9c & 0x03FF0000 != 0x00420000:
        result |= 0x01
    else:
        _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
        _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 11, 0)
        return result
    val32 = 0x80007C00 | (reg_e94 & 0x03FF0000) | ((reg_e9c & 0x03FF0000) >> 16)
    transport.write32(REG_TX_IQK, val32)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 1, 1)
    write_rfreg(transport, RF_A, RF6052_REG_GAIN_P1, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_PAD_TXG, 0x003FF, 0x1E0)
    transport.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)
    transport.write32(REG_ANAPWR1, 0x44FFBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x00400040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000100)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x18008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_TX_IQK_PI_A, 0x82170000)
    transport.write32(REG_RX_IQK_PI_A, 0x28170000)
    transport.write32(REG_RX_IQK, 0x01004800)
    transport.write32(REG_IQK_AGC_RSP, 0x0046A8D1)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_RXA)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = transport.read32(REG_RX_POWER_BEFORE_IQK_A_2)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_A, RF6052_REG_GAIN_CCA, 1 << 11, 0)
    write_rfreg(transport, RF_A, RF6052_REG_GAIN_P1, 0x02000)
    if not reg_eac & (1 << 27) and reg_ea4 & 0x03FF0000 != 0x01320000 and reg_eac & 0x03FF0000 != 0x00360000:
        result |= 0x02
    return result


def _rtl8192fu_iqk_path_b(transport, rfe_type: int) -> int:
    result = 0
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    transport.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)
    transport.write32(REG_ANAPWR1, 0x44FFBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x00400040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000000)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 4, 1)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    if rfe_type in (7, 8, 9, 12):
        _write_rfreg_mask(transport, RF_B, RF6052_REG_PAD_TXG, 0x003FF, 0x30)
    else:
        _write_rfreg_mask(transport, RF_B, RF6052_REG_PAD_TXG, 0x00FFF, 0xE9)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x18008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_TX_IQK_PI_B, 0x8214000F)
    transport.write32(REG_RX_IQK_PI_B, 0x28140000)
    transport.write32(REG_TX_IQK, 0x01007C00)
    transport.write32(REG_RX_IQK, 0x01004800)
    transport.write32(REG_IQK_AGC_RSP, 0x00E62911)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_TXB)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = transport.read32(REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = transport.read32(REG_TX_POWER_AFTER_IQK_B)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_WE_LUT, 1 << 4, 1)
    val32 = read_rfreg(transport, RF_B, RF6052_REG_TXMOD)
    rf_0x58_i = (val32 & 0xFC000) >> 14
    rf_0x58_q = (val32 & 0x003F0) >> 4
    for i in range(8):
        _write_rfreg_mask(transport, RF_B, RF6052_REG_TXPA_G3, 0x1C000, i)
        _write_rfreg_mask(transport, RF_B, RF6052_REG_TXPA_G3, 0x00FC0, rf_0x58_i)
        _write_rfreg_mask(transport, RF_B, RF6052_REG_TXPA_G3, 0x0003F, rf_0x58_q)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_AC, 1 << 14, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_WE_LUT, 1 << 4, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 0x00810, 0)
    if not reg_eac & (1 << 31) and reg_eb4 & 0x03FF0000 != 0x01420000 and reg_ebc & 0x03FF0000 != 0x00420000:
        result |= 0x01
    return result


def _rtl8192fu_rx_iqk_path_b(transport) -> int:
    result = 0
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 1, 1)
    write_rfreg(transport, RF_B, RF6052_REG_GAIN_P1, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_PAD_TXG, 0x003FF, 0x67)
    transport.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)
    transport.write32(REG_ANAPWR1, 0x44FFBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x00400040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000000)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x18008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_TX_IQK_PI_B, 0x82160027)
    transport.write32(REG_RX_IQK_PI_B, 0x28160000)
    transport.write32(REG_IQK_AGC_RSP, 0x0086A911)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_TXB)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = transport.read32(REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = transport.read32(REG_TX_POWER_AFTER_IQK_B)
    if not reg_eac & (1 << 31) and reg_eb4 & 0x03FF0000 != 0x01420000 and reg_ebc & 0x03FF0000 != 0x00420000:
        result |= 0x01
    else:
        _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
        _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 11, 0)
        return result
    val32 = 0x80007C00 | (reg_eb4 & 0x03FF0000) | ((reg_ebc >> 16) & 0x03FF)
    transport.write32(REG_TX_IQK, val32)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 1, 1)
    write_rfreg(transport, RF_B, RF6052_REG_GAIN_P1, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 11, 1)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_PAD_TXG, 0x003FF, 0x1E0)
    transport.write32(REG_FPGA0_ANALOG4, 0xCCF000C0)
    transport.write32(REG_ANAPWR1, 0x44FFBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x00400040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000000)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    transport.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    transport.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    transport.write32(REG_RX_IQK_TONE_B, 0x18008C1C)
    transport.write32(REG_TX_IQK_PI_B, 0x82170000)
    transport.write32(REG_RX_IQK_PI_B, 0x28170000)
    transport.write32(REG_RX_IQK, 0x01004800)
    transport.write32(REG_IQK_AGC_RSP, 0x0046A911)
    transport.write32(REG_IQK_AGC_PTS, 0xFA005800)
    transport.write32(REG_IQK_AGC_PTS, 0xF8005800)
    _wait_iqk_report(transport, REG_IQK_RPT_RXB)
    reg_eac = transport.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ec4 = transport.read32(REG_RX_POWER_BEFORE_IQK_B_2)
    reg_ecc = transport.read32(REG_RX_POWER_AFTER_IQK_B_2)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000100)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 11, 0)
    _write_rfreg_mask(transport, RF_B, RF6052_REG_GAIN_CCA, 1 << 1, 0)
    write_rfreg(transport, RF_B, RF6052_REG_GAIN_P1, 0x02000)
    if not reg_eac & (1 << 30) and reg_ec4 & 0x03FF0000 != 0x01320000 and reg_ecc & 0x03FF0000 != 0x00360000:
        result |= 0x02
    return result


def _save_mac_regs(transport, registers: tuple[int, ...]) -> list[int]:
    return [transport.read8(register) for register in registers[:-1]] + [transport.read32(registers[-1])]


def _restore_mac_regs(transport, registers: tuple[int, ...], backup: list[int]) -> None:
    for register, value in zip(registers[:-1], backup[:-1], strict=True):
        transport.write8(register, value)
    transport.write32(registers[-1], backup[-1])


def _save_regs(transport, registers: tuple[int, ...]) -> list[int]:
    return [transport.read32(register) for register in registers]


def _restore_regs(transport, registers: tuple[int, ...], backup: list[int]) -> None:
    for register, value in zip(registers, backup, strict=True):
        transport.write32(register, value)


def _rtl8192fu_phy_iqcalibrate(transport, result: list[list[int]], t: int, rfe_type: int, backups: dict) -> None:
    adda_regs = (REG_ANAPWR1, REG_RX_WAIT_CCA)
    iqk_mac_regs = (REG_TXPAUSE, REG_BEACON_CTRL, REG_BEACON_CTRL_1, REG_GPIO_MUXCFG)
    iqk_bb_regs = (
        REG_OFDM0_TRX_PATH_ENABLE,
        REG_OFDM0_TR_MUX_PAR,
        REG_FPGA0_XCD_RF_SW_CTRL,
        REG_CONFIG_ANT_A,
        REG_CONFIG_ANT_B,
        REG_DPDT_CTRL,
        REG_RFE_CTRL_ANTA_SRC,
        REG_RFE_CTRL_ANT_SRC2,
        REG_CCK0_AFE_SETTING,
    )
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    rx_initial_gain_a = transport.read32(REG_OFDM0_XA_AGC_CORE1)
    rx_initial_gain_b = transport.read32(REG_OFDM0_XB_AGC_CORE1)
    if t == 0:
        backups["adda"] = _save_regs(transport, adda_regs)
        backups["mac"] = _save_mac_regs(transport, iqk_mac_regs)
        backups["bb"] = _save_regs(transport, iqk_bb_regs)
    _write32_set(transport, REG_FPGA0_XCD_RF_PARM, 1 << 31)
    transport.write8(REG_TXPAUSE, 0xFF)
    _write8_clear(transport, REG_GPIO_MUXCFG, 1 << 5)
    if rfe_type in (7, 8, 9, 12):
        _write32_mask(transport, REG_RFE_CTRL_ANTA_SRC, 0xF, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x1, 0)
        _write32_mask(transport, REG_RFE_CTRL_ANTA_SRC, 0xF00, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x4, 0)
        _write32_mask(transport, REG_RFE_CTRL_ANTA_SRC, 0xF000, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x8, 0)
        _write32_mask(transport, REG_RFE_CTRL_ANT_SRC2, 0xF0, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x20000, 0)
        _write32_mask(transport, REG_RFE_CTRL_ANT_SRC2, 0xF0000, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x100000, 0)
        _write32_mask(transport, REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x7)
        _write32_mask(transport, REG_DPDT_CTRL, 0x8000000, 0)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    write_rfreg(transport, RF_B, RF6052_REG_AC, 0x10000)
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)
    for _ in range(2):
        path_a_ok = _rtl8192fu_iqk_path_a(transport, rfe_type)
        if path_a_ok == 0x01:
            result[t][0] = (transport.read32(REG_TX_POWER_BEFORE_IQK_A) >> 16) & 0x3FF
            result[t][1] = (transport.read32(REG_TX_POWER_AFTER_IQK_A) >> 16) & 0x3FF
            break
        result[t][0:2] = [0x100, 0]
    for _ in range(2):
        path_a_ok = _rtl8192fu_rx_iqk_path_a(transport)
        if path_a_ok == 0x03:
            result[t][2] = (transport.read32(REG_RX_POWER_BEFORE_IQK_A_2) >> 16) & 0x3FF
            result[t][3] = (transport.read32(REG_RX_POWER_AFTER_IQK_A_2) >> 16) & 0x3FF
            break
        result[t][2:4] = [0x100, 0]
    for _ in range(2):
        path_b_ok = _rtl8192fu_iqk_path_b(transport, rfe_type)
        if path_b_ok == 0x01:
            result[t][4] = (transport.read32(REG_TX_POWER_BEFORE_IQK_B) >> 16) & 0x3FF
            result[t][5] = (transport.read32(REG_TX_POWER_AFTER_IQK_B) >> 16) & 0x3FF
            break
        result[t][4:6] = [0x100, 0]
    for _ in range(2):
        path_b_ok = _rtl8192fu_rx_iqk_path_b(transport)
        if path_b_ok == 0x03:
            result[t][6] = (transport.read32(REG_RX_POWER_BEFORE_IQK_B_2) >> 16) & 0x3FF
            result[t][7] = (transport.read32(REG_RX_POWER_AFTER_IQK_B_2) >> 16) & 0x3FF
            break
        result[t][6:8] = [0x100, 0]
    _write32_mask(transport, REG_FPGA0_IQK, 0xFFFFFF00, 0)
    transport.write32(REG_FPGA0_ANALOG4, 0xCC0000C0)
    transport.write32(REG_ANAPWR1, 0x44BBBB44)
    transport.write32(REG_RX_WAIT_CCA, 0x80408040)
    transport.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x6F005433)
    transport.write32(REG_OFDM0_TR_MUX_PAR, 0x000004E4)
    transport.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x04003400)
    transport.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000100)
    _restore_regs(transport, adda_regs, backups["adda"])
    _restore_mac_regs(transport, iqk_mac_regs, backups["mac"])
    _restore_regs(transport, iqk_bb_regs, backups["bb"])
    _write32_clear(transport, REG_FPGA0_XCD_RF_PARM, 1 << 31)
    _write32_mask(transport, REG_OFDM0_XA_AGC_CORE1, 0xFF, 0x50)
    _write32_mask(transport, REG_OFDM0_XA_AGC_CORE1, 0xFF, rx_initial_gain_a & 0xFF)
    _write32_mask(transport, REG_OFDM0_XB_AGC_CORE1, 0xFF, 0x50)
    _write32_mask(transport, REG_OFDM0_XB_AGC_CORE1, 0xFF, rx_initial_gain_b & 0xFF)


def _sign_extend_10(value: int) -> int:
    return value - 0x400 if value & 0x200 else value


def _rtl8xxxu_gen2_simularity_compare(result: list[list[int]], c1: int, c2: int) -> bool:
    candidate = [-1, -1]
    simubitmap = 0
    for i in range(8):
        tmp1 = _sign_extend_10(result[c1][i]) if i & 1 else result[c1][i]
        tmp2 = _sign_extend_10(result[c2][i]) if i & 1 else result[c2][i]
        diff = abs(tmp1 - tmp2)
        if diff > MAX_TOLERANCE:
            if i in (2, 6) and not simubitmap:
                if result[c1][i] + result[c1][i + 1] == 0:
                    candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    candidate[i // 4] = c1
                else:
                    simubitmap |= 1 << i
            else:
                simubitmap |= 1 << i
    if simubitmap == 0:
        retval = True
        for i in range(2):
            if candidate[i] >= 0:
                result[3][i * 4 : (i + 1) * 4 - 2] = result[candidate[i]][i * 4 : (i + 1) * 4 - 2]
                retval = False
        return retval
    if not simubitmap & 0x03:
        result[3][0:2] = result[c1][0:2]
    if not simubitmap & 0x0C:
        result[3][2:4] = result[c1][2:4]
    if not simubitmap & 0x30:
        result[3][4:6] = result[c1][4:6]
    if not simubitmap & 0xC0:
        result[3][6:8] = result[c1][6:8]
    return False


def _rtl8xxxu_fill_iqk_matrix_a(transport, result: list[list[int]], candidate: int, tx_only: bool) -> None:
    oldval = transport.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE) >> 22
    x = _sign_extend_10(result[candidate][0])
    tx0_a = (x * oldval) >> 8
    val32 = transport.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, (val32 & ~0x3FF) | (tx0_a & 0x3FF))
    val32 = transport.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 31)
    if (x * oldval >> 7) & 1:
        val32 |= 1 << 31
    transport.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)
    y = _sign_extend_10(result[candidate][1])
    tx0_c = (y * oldval) >> 8
    val32 = transport.read32(REG_OFDM0_XC_TX_AFE)
    transport.write32(REG_OFDM0_XC_TX_AFE, (val32 & ~0xF0000000) | (((tx0_c & 0x3C0) >> 6) << 28))
    val32 = transport.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, (val32 & ~0x003F0000) | ((tx0_c & 0x3F) << 16))
    val32 = transport.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 29)
    if (y * oldval >> 7) & 1:
        val32 |= 1 << 29
    transport.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)
    if tx_only:
        return
    reg = result[candidate][2]
    val32 = transport.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, (val32 & ~0x3FF) | (reg & 0x3FF))
    reg = result[candidate][3] & 0x3F
    val32 = transport.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, (val32 & ~0xFC00) | ((reg << 10) & 0xFC00))
    reg = (result[candidate][3] >> 6) & 0xF
    val32 = transport.read32(REG_OFDM0_RX_IQ_EXT_ANTA)
    transport.write32(REG_OFDM0_RX_IQ_EXT_ANTA, (val32 & ~0xF0000000) | (reg << 28))


def _rtl8xxxu_fill_iqk_matrix_b(transport, result: list[list[int]], candidate: int, tx_only: bool) -> None:
    oldval = transport.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE) >> 22
    x = _sign_extend_10(result[candidate][4])
    tx1_a = (x * oldval) >> 8
    val32 = transport.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XB_TX_IQ_IMBALANCE, (val32 & ~0x3FF) | (tx1_a & 0x3FF))
    val32 = transport.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 27)
    if (x * oldval >> 7) & 1:
        val32 |= 1 << 27
    transport.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)
    y = _sign_extend_10(result[candidate][5])
    tx1_c = (y * oldval) >> 8
    val32 = transport.read32(REG_OFDM0_XD_TX_AFE)
    transport.write32(REG_OFDM0_XD_TX_AFE, (val32 & ~0xF0000000) | (((tx1_c & 0x3C0) >> 6) << 28))
    val32 = transport.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XB_TX_IQ_IMBALANCE, (val32 & ~0x003F0000) | ((tx1_c & 0x3F) << 16))
    val32 = transport.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 25)
    if (y * oldval >> 7) & 1:
        val32 |= 1 << 25
    transport.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)
    if tx_only:
        return
    reg = result[candidate][6]
    val32 = transport.read32(REG_OFDM0_XB_RX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XB_RX_IQ_IMBALANCE, (val32 & ~0x3FF) | (reg & 0x3FF))
    reg = result[candidate][7] & 0x3F
    val32 = transport.read32(REG_OFDM0_XB_RX_IQ_IMBALANCE)
    transport.write32(REG_OFDM0_XB_RX_IQ_IMBALANCE, (val32 & ~0xFC00) | ((reg << 10) & 0xFC00))
    reg = (result[candidate][7] >> 6) & 0xF
    _write32_mask(transport, REG_RXIQB_EXT, 0x000000F0, reg)


def phy_iq_calibrate(transport, rfe_type: int) -> None:
    rfe_path_select = transport.read32(REG_RFE_PATH_SELECT)
    path_a_0xdf = read_rfreg(transport, RF_A, RF6052_REG_GAIN_CCA)
    path_a_0x35 = read_rfreg(transport, RF_A, RF6052_REG_GAIN_P1)
    path_b_0xdf = read_rfreg(transport, RF_B, RF6052_REG_GAIN_CCA)
    path_b_0x35 = read_rfreg(transport, RF_B, RF6052_REG_GAIN_P1)
    result = [[0] * 8 for _ in range(4)]
    backups = {}
    candidate = -1
    for i in range(3):
        _rtl8192fu_phy_iqcalibrate(transport, result, i, rfe_type, backups)
        if i == 1 and _rtl8xxxu_gen2_simularity_compare(result, 0, 1):
            candidate = 0
            break
        if i == 2:
            if _rtl8xxxu_gen2_simularity_compare(result, 0, 2):
                candidate = 0
                break
            if _rtl8xxxu_gen2_simularity_compare(result, 1, 2):
                candidate = 1
            elif sum(result[3]):
                candidate = 3
    _write32_mask(transport, REG_TX_IQK_TONE_A, 0x3FF00000, 0x100)
    _write32_mask(transport, REG_NP_ANTA, 0x3FF, 0)
    _write32_mask(transport, REG_TX_IQK_TONE_B, 0x3FF00000, 0x100)
    _write32_mask(transport, REG_TAP_UPD_97F, 0x3FF, 0)
    if candidate >= 0:
        reg_e94 = result[candidate][0]
        reg_ea4 = result[candidate][2]
        reg_eb4 = result[candidate][4]
        reg_ec4 = result[candidate][6]
        if reg_e94:
            _rtl8xxxu_fill_iqk_matrix_a(transport, result, candidate, reg_ea4 == 0)
        if reg_eb4:
            _rtl8xxxu_fill_iqk_matrix_b(transport, result, candidate, reg_ec4 == 0)
    write_rfreg(transport, RF_A, RF6052_REG_GAIN_CCA, path_a_0xdf)
    write_rfreg(transport, RF_A, RF6052_REG_GAIN_P1, path_a_0x35)
    write_rfreg(transport, RF_B, RF6052_REG_GAIN_CCA, path_b_0xdf)
    write_rfreg(transport, RF_B, RF6052_REG_GAIN_P1, path_b_0x35)
    if rfe_type in (7, 8, 9, 12):
        _write32_set(transport, REG_SW_GPIO_SHARE_CTRL_1, 0x70000)
        _write32_clear(transport, REG_LEDCFG0, 0x6C00000)
        _write32_set(transport, REG_PAD_CTRL1, (1 << 29) | (1 << 28))
        _write32_clear(transport, REG_SW_GPIO_SHARE_CTRL_0, 0x600000 | (1 << 4))
        val32 = transport.read32(REG_RFE_BUFFER)
        transport.write32(REG_RFE_BUFFER, (val32 & ~(1 << 11)) | 0x1F)
        if rfe_type == 7:
            _write32_mask(transport, REG_RFE_CTRL_ANTA_SRC, 0xFFFFF, 0x23200)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC2, 0xFFFFF, 0x23200)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC1, 0xF000, 0x3)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x3)
        else:
            _write32_mask(transport, REG_RFE_CTRL_ANTA_SRC, 0xFFFFF, 0x22200)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC2, 0xFFFFF, 0x22200)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC1, 0xF000, 0x2)
            _write32_mask(transport, REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x2)
        _write32_clear(transport, REG_RFE_OPT62, 1 << 2)
        if rfe_type == 7:
            transport.write32(REG_RFE_OPT, 0x03000003)
        transport.write32(REG_RFE_PATH_SELECT, rfe_path_select)
