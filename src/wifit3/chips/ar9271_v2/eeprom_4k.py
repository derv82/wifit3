"""Typed view over the filled 4k EEPROM map (``struct ar5416_eeprom_4k``).

The bytes in ``hw.eeprom`` are the raw little-endian image of ``struct ar5416_eeprom_4k``
(``__packed``), 376 bytes / 188 words read from word 64. This module decodes only the fields
the TX-power computation needs; offsets are derived 1:1 from the packed layout in
``data_dumps/ath9k-source-v6.18.12/eeprom.h`` (base_eep_header_4k 32B, custData 20B,
modal_eep_4k_header 68B, then the cal/target/ctl arrays).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import reg as R

# packed-struct byte offsets within map4k
_MODAL = 52                            # base_eep_header_4k(32) + custData(20)
_OFF_VERSION = 4                       # baseEepHeader.version (__le16)
_OFF_TXMASK = 19                       # baseEepHeader.txMask
_OFF_ANTGAIN = _MODAL + 8             # modalHeader.antennaGainCh[0]
_OFF_XPDGAIN = _MODAL + 20            # modalHeader.xpdGain
_OFF_PDGAINOVERLAP = _MODAL + 24      # modalHeader.pdGainOverlap
_OFF_HT40INC = _MODAL + 30            # modalHeader.ht40PowerIncForPdadc
_CALFREQPIER2G = 120                  # after modal header (52+68)
_CALPIERDATA2G = 123                  # 1 chain x 3 piers, cal_data_per_freq_4k = 20B each
_CALTARGET_CCK = 183                  # 3 x cal_target_power_leg (5B)
_CALTARGET_2G = 198                   # 3 x cal_target_power_leg (5B)
_CALTARGET_2GHT20 = 213               # 3 x cal_target_power_ht (9B)
_CTLINDEX = 267                       # ctlIndex[12]
_CTLDATA = 279                        # 12 x cal_ctl_data_4k (1 chain x 4 edges x 2B = 8B)
_SPURCHANS = _MODAL + 48              # modalHeader.spurChans[5], spur_chan = 4B each


@dataclass
class CalTargetLeg:
    bChannel: int
    tPow2x: list[int]                 # 4 rates


@dataclass
class CalTargetHt:
    bChannel: int
    tPow2x: list[int]                 # 8 rates


@dataclass
class CalPier4k:
    """cal_data_per_freq_4k: pwrPdg[2][5] then vpdPdg[2][5] (u8)."""
    pwrPdg: list[list[int]]
    vpdPdg: list[list[int]]


class Map4k:
    """Decoded fields of ``ah->eeprom.map4k`` used by the TX-power path."""

    def __init__(self, raw: bytes | bytearray):
        self.raw = bytes(raw)

    def _u8(self, off: int) -> int:
        return self.raw[off]

    def _le16(self, off: int) -> int:
        return struct.unpack_from("<H", self.raw, off)[0]

    @property
    def version(self) -> int:
        return self._le16(_OFF_VERSION)

    @property
    def eeprom_rev(self) -> int:               # ath9k_hw_4k_get_eeprom_rev [SRC] eeprom_4k.c:29
        return self.version & R.AR5416_EEP_VER_MINOR_MASK

    @property
    def txMask(self) -> int:
        return self._u8(_OFF_TXMASK)

    @property
    def antennaGainCh0(self) -> int:           # EEP_ANTENNA_GAIN_2G [SRC] eeprom_4k.c:275
        return self._u8(_OFF_ANTGAIN)

    @property
    def xpdGain(self) -> int:
        return self._u8(_OFF_XPDGAIN)

    @property
    def pdGainOverlap(self) -> int:
        return self._u8(_OFF_PDGAINOVERLAP)

    @property
    def ht40PowerIncForPdadc(self) -> int:
        return self._u8(_OFF_HT40INC)

    @property
    def calFreqPier2G(self) -> list[int]:
        return [self._u8(_CALFREQPIER2G + i) for i in range(R.AR5416_EEP4K_NUM_2G_CAL_PIERS)]

    def calPierData2G(self) -> list[CalPier4k]:
        """calPierData2G[chain0][pier] — 3 piers for the single 9271 chain."""
        out: list[CalPier4k] = []
        ng, ni = R.AR5416_EEP4K_NUM_PD_GAINS, R.AR5416_PD_GAIN_ICEPTS
        for p in range(R.AR5416_EEP4K_NUM_2G_CAL_PIERS):
            base = _CALPIERDATA2G + p * (ng * ni * 2)
            pwr = [[self._u8(base + g * ni + k) for k in range(ni)] for g in range(ng)]
            vpd = [[self._u8(base + ng * ni + g * ni + k) for k in range(ni)] for g in range(ng)]
            out.append(CalPier4k(pwr, vpd))
        return out

    def _legs(self, base: int, n: int) -> list[CalTargetLeg]:
        return [CalTargetLeg(self._u8(base + i * 5),
                             [self._u8(base + i * 5 + 1 + k) for k in range(4)]) for i in range(n)]

    @property
    def calTargetPowerCck(self) -> list[CalTargetLeg]:
        return self._legs(_CALTARGET_CCK, 3)

    @property
    def calTargetPower2G(self) -> list[CalTargetLeg]:
        return self._legs(_CALTARGET_2G, 3)

    @property
    def calTargetPower2GHT20(self) -> list[CalTargetHt]:
        return [CalTargetHt(self._u8(_CALTARGET_2GHT20 + i * 9),
                            [self._u8(_CALTARGET_2GHT20 + i * 9 + 1 + k) for k in range(8)])
                for i in range(3)]

    @property
    def ctlIndex(self) -> list[int]:
        return [self._u8(_CTLINDEX + i) for i in range(R.AR5416_EEP4K_NUM_CTLS)]

    def ctlEdges(self, i: int) -> list[tuple[int, int]]:
        """ctlData[i].ctlEdges[chain0][edge] -> (bChannel, ctl) for the 4 band edges."""
        base = _CTLDATA + i * 8
        return [(self._u8(base + 2 * k), self._u8(base + 2 * k + 1))
                for k in range(R.AR5416_EEP4K_NUM_BAND_EDGES)]

    def get_spur_channel(self, i: int) -> int:
        """modalHeader.spurChans[i].spurChan (__le16) [SRC] eeprom_4k.c get_spur_channel."""
        return self._le16(_SPURCHANS + i * 4)

    # ---- modal header fields used by set_board_values (offsets from _MODAL) -----
    @property
    def antCtrlChain0(self) -> int:
        return struct.unpack_from("<I", self.raw, _MODAL + 0)[0]

    @property
    def antCtrlCommon(self) -> int:
        return struct.unpack_from("<I", self.raw, _MODAL + 4)[0]

    @property
    def switchSettling(self) -> int:
        return self._u8(_MODAL + 9)

    @property
    def txRxAttenCh0(self) -> int:
        return self._u8(_MODAL + 10)

    @property
    def rxTxMarginCh0(self) -> int:
        return self._u8(_MODAL + 11)

    @property
    def adcDesiredSize(self) -> int:
        return self._u8(_MODAL + 12)

    @property
    def txEndToXpaOff(self) -> int:
        return self._u8(_MODAL + 15)

    @property
    def txEndToRxOn(self) -> int:
        return self._u8(_MODAL + 16)

    @property
    def txFrameToXpaOn(self) -> int:
        return self._u8(_MODAL + 17)

    @property
    def thresh62(self) -> int:
        return self._u8(_MODAL + 18)

    @property
    def iqCalICh0(self) -> int:
        return self._u8(_MODAL + 22)

    @property
    def iqCalQCh0(self) -> int:
        return self._u8(_MODAL + 23)

    @property
    def modal_version(self) -> int:
        return self._u8(_MODAL + 37)

    @property
    def txFrameToDataStart(self) -> int:
        return self._u8(_MODAL + 28)

    @property
    def txFrameToPaOn(self) -> int:
        return self._u8(_MODAL + 29)

    @property
    def bswAtten0(self) -> int:
        return self._u8(_MODAL + 31)

    @property
    def bswMargin0(self) -> int:
        return self._u8(_MODAL + 32)

    @property
    def xatten2Db0(self) -> int:
        return self._u8(_MODAL + 34)

    @property
    def xatten2Margin0(self) -> int:
        return self._u8(_MODAL + 35)

    @property
    def bb_scale_smrt_antenna(self) -> int:
        return self._u8(_MODAL + 46)

    # nibble-packed bitfields (little-endian: low nibble first) [SRC] eeprom.h:407-440
    @property
    def ob(self) -> list[int]:
        b77, b90, b91 = self._u8(_MODAL + 25), self._u8(_MODAL + 38), self._u8(_MODAL + 39)
        return [b77 & 0xf, (b77 >> 4) & 0xf, b90 & 0xf, (b90 >> 4) & 0xf, b91 & 0xf]

    @property
    def db1(self) -> list[int]:
        b78, b92, b93 = self._u8(_MODAL + 26), self._u8(_MODAL + 40), self._u8(_MODAL + 41)
        return [b78 & 0xf, (b78 >> 4) & 0xf, b92 & 0xf, (b92 >> 4) & 0xf, b93 & 0xf]

    @property
    def db2(self) -> list[int]:
        b88, b94, b95 = self._u8(_MODAL + 36), self._u8(_MODAL + 42), self._u8(_MODAL + 43)
        return [b88 & 0xf, (b88 >> 4) & 0xf, b94 & 0xf, (b94 >> 4) & 0xf, b95 & 0xf]

    @property
    def antdiv_ctl1(self) -> int:
        return (self._u8(_MODAL + 39) >> 4) & 0xf

    @property
    def antdiv_ctl2(self) -> int:
        return (self._u8(_MODAL + 41) >> 4) & 0xf
