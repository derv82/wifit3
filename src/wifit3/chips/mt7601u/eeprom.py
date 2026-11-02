"""EEPROM/efuse read and decode for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/eeprom.c (tag v7.2).

The 256-byte parameter block is read at runtime in 16-byte chunks; every value the
rest of the driver consumes comes out of here. Nothing here may be hardcoded to a
particular card -- the region plan, RX/TX stream count, TSSI mode and per-rate
power table are all EEPROM-derived, and a sibling with different bytes must work.

We only READ. mt7601u never programs efuse fuses.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .constants import (
    MAX_PWR,
    MT7601U_DEFAULT_TX_POWER,
    MT7601U_EE_MAX_VER,
    MT7601U_EEPROM_SIZE,
    MT_EE_COUNTRY_REGION,
    MT_EE_FREQ_OFFSET,
    MT_EE_FREQ_OFFSET_COMPENSATION,
    MT_EE_LNA_GAIN,
    MT_EE_MAC_ADDR,
    MT_EE_NIC_CONF_0,
    MT_EE_NIC_CONF_0_RX_PATH,
    MT_EE_NIC_CONF_0_TX_PATH,
    MT_EE_NIC_CONF_1,
    MT_EE_NIC_CONF_1_HW_RF_CTRL,
    MT_EE_NIC_CONF_1_TEMP_TX_ALC,
    MT_EE_NIC_CONF_1_TX_ALC_EN,
    MT_EE_REF_TEMP,
    MT_EE_RSSI_OFFSET,
    MT_EE_TX_POWER_DELTA_BW40,
    MT_EE_TX_POWER_OFFSET,
    MT_EE_TX_POWER_BYRATE,
    MT_EE_TX_TSSI_OFFSET,
    MT_EE_TX_TSSI_OFFSET_GROUP,
    MT_EE_TX_TSSI_SLOPE,
    MT_EE_TX_TSSI_TARGET_POWER,
    MT_EE_USAGE_MAP_START,
    MT_EE_VERSION_EE,
    MT_EE_VERSION_FAE,
    MT_EFUSE_CTRL,
    MT_EFUSE_CTRL_AIN,
    MT_EFUSE_CTRL_AOUT,
    MT_EFUSE_CTRL_KICK,
    MT_EFUSE_CTRL_MODE,
    MT_EE_PHYSICAL_READ,
    MT_EE_READ,
    MT_EFUSE_USAGE_MAP_SIZE,
    MT_TX_ALC_CFG_0,
    MT_TX_ALC_CFG_0_LIMIT_0,
    MT_MAC_ADDR_DW0,
    MT_MAC_ADDR_DW1,
    MT_MAC_ADDR_DW1_U2ME_MASK,
    MT_TX_PWR_CFG_0,
    MT_TX_PWR_CFG_1,
    MT_TX_PWR_CFG_2,
    MT_TX_PWR_CFG_4,
    MT_TX_PWR_CFG_7,
    MT_TX_PWR_CFG_9,
    N_CHAN_PWR,
    N_RATE_POWER_GROUPS,
    S6_MODULUS,
    S6_SIGN_BIT,
    _field_get,
    _field_prep,
    MT_EFUSE_DATA,
)
from .transport import MT7601UTransport

logger = logging.getLogger(__name__)

_EFUSE_READ_TIMEOUT_US = 1000       # eeprom.c:46 mt76_poll timeout
_MAP_READS = (MT_EFUSE_USAGE_MAP_SIZE + 15) // 16   # eeprom.c:69 DIV_ROUND_UP(..., 16)
_INVALID_FIELD = 0xFF                # eeprom.c:19 field_valid
_DEFAULT_TARGET_PWR = 0x20           # eeprom.c:138 fallback for a bad TSSI target


@dataclass
class PowerPerRate:
    """eeprom.h:76 -- one s6 value plus its sign-extended 20/40 MHz deltas."""
    raw: int = 0
    bw20: int = 0
    bw40: int = 0


@dataclass
class MT7601URatePower:
    """eeprom.h:82 -- power per rate, two rates per EEPROM word (CCK[2]/OFDM[4]/HT[4])."""
    cck: list[PowerPerRate] = field(default_factory=lambda: [PowerPerRate() for _ in range(2)])
    ofdm: list[PowerPerRate] = field(default_factory=lambda: [PowerPerRate() for _ in range(4)])
    ht: list[PowerPerRate] = field(default_factory=lambda: [PowerPerRate() for _ in range(4)])


@dataclass
class RegChannelBounds:
    """eeprom.h:88 -- the first channel and how many the EEPROM's region allows."""
    start: int = 1
    num: int = 14


@dataclass
class MT7601UTssiData:
    """eeprom.h:105 -- only meaningful when TSSI (internal TX ALC) is enabled."""
    tx0_delta_offset: int = 0
    slope: int = 0
    offset: list[int] = field(default_factory=lambda: [0, 0, 0])


@dataclass
class MT7601UEepromParams:
    """eeprom.h:93 -- every EEPROM-derived value the rest of the driver consumes."""
    tssi_enabled: bool = False
    rf_freq_off: int = 0
    rssi_offset: list[int] = field(default_factory=lambda: [0, 0])
    ref_temp: int = 0
    lna_gain: int = 0
    chan_pwr: list[int] = field(default_factory=lambda: [0] * N_CHAN_PWR)
    power_rate_table: MT7601URatePower = field(default_factory=MT7601URatePower)
    real_cck_bw20: list[int] = field(default_factory=lambda: [0, 0])
    tssi_data: MT7601UTssiData = field(default_factory=MT7601UTssiData)
    reg: RegChannelBounds = field(default_factory=RegChannelBounds)


class EepromError(Exception):
    """The EEPROM read failed or the device needs a default EEPROM we don't ship."""


def field_valid(val: int) -> bool:
    """eeprom.c:17 -- 0xff marks an unwritten EEPROM field."""
    return val != _INVALID_FIELD


def _s8(val: int) -> int:
    """Sign-extend an EEPROM byte into the s8 fields eeprom.h:96-98 declares."""
    return val - 0x100 if val & 0x80 else val


def _is_valid_ether_addr(addr: bytes) -> bool:
    """is_valid_ether_addr (mac.c:15) -- not a group address and not all-zero.

    Broadcast needs no separate test: ff:ff:ff:ff:ff:ff has the group bit set.
    """
    return len(addr) == 6 and not addr[0] & 0x01 and addr != bytes(6)


def _random_ether_addr() -> bytes:
    """eth_random_addr -- random bytes with the group bit cleared and the
    locally-administered bit set."""
    addr = bytearray(os.urandom(6))
    addr[0] = (addr[0] & 0xFE) | 0x02
    return bytes(addr)


def field_validate(val: int) -> int:
    """eeprom.c:22 -- an unwritten field reads as 0."""
    return val if field_valid(val) else 0


def s6_validate(reg: int) -> int:
    """eeprom.h:116 -- clamp to the 6-bit s6 power encoding."""
    return reg & MAX_PWR


def s6_to_int(reg: int) -> int:
    """eeprom.h:122 -- sign-extend a 6-bit s6 value into a signed int."""
    s6 = s6_validate(reg)
    return s6 - S6_MODULUS if s6 & S6_SIGN_BIT else s6


def int_to_s6(val: int) -> int:
    """eeprom.h:133 -- clamp a signed int into the 6-bit s6 encoding."""
    if val < -0x20:
        return 0x20
    if val > 0x1F:
        return 0x1F
    return val & 0x3F


class MT7601UEeprom:
    """Reads and decodes the EEPROM block over the transport."""

    def __init__(self, tp: MT7601UTransport, ee: MT7601UEepromParams | None = None):
        self.tp = tp
        self.ee = MT7601UEepromParams() if ee is None else ee
        self.raw = bytearray(MT7601U_EEPROM_SIZE)
        self.macaddr = bytes(6)
        self.version_ee = 0
        self.version_fae = 0

    # ------------------------------------------------------------------
    # efuse access (eeprom.c:32)
    # ------------------------------------------------------------------

    def efuse_read(self, addr: int, mode: int) -> bytes:
        """Read 16 efuse bytes at ``addr``.

        One KICK, then poll KICK clear, then four MT_EFUSE_DATA reads. Regions
        outside the usage map return all-ones (eeprom.c:54), which is not an error.
        """
        val = self.tp.rr(MT_EFUSE_CTRL)
        val &= ~(MT_EFUSE_CTRL_AIN | MT_EFUSE_CTRL_MODE) & 0xFFFFFFFF
        val |= (_field_prep(MT_EFUSE_CTRL_AIN, addr & ~0xF)
                | _field_prep(MT_EFUSE_CTRL_MODE, mode)
                | MT_EFUSE_CTRL_KICK)
        self.tp.wr(MT_EFUSE_CTRL, val)

        if not self._poll_efuse_done():
            raise EepromError(f"efuse read timed out at {addr:#06x}")

        val = self.tp.rr(MT_EFUSE_CTRL)
        if (val & MT_EFUSE_CTRL_AOUT) == MT_EFUSE_CTRL_AOUT:
            return b"\xff" * 16

        out = bytearray()
        for i in range(4):
            out += self.tp.rr(MT_EFUSE_DATA(i)).to_bytes(4, "little")
        return bytes(out)

    def _poll_efuse_done(self) -> bool:
        """mt76_poll(dev, MT_EFUSE_CTRL, MT_EFUSE_CTRL_KICK, 0, 1000) (eeprom.c:46)."""
        timeout = _EFUSE_READ_TIMEOUT_US // 10
        while True:
            if self.tp.rr(MT_EFUSE_CTRL) & MT_EFUSE_CTRL_KICK == 0:
                return True
            if timeout <= 0:
                return False
            timeout -= 1

    # ------------------------------------------------------------------
    # Decoders
    # ------------------------------------------------------------------

    def set_macaddr(self, addr: bytes) -> None:
        """mac.c:11 mt7601u_set_macaddr -- program the card's own MAC.

        A group or all-zero EEPROM address is unusable, and mac.c:16 installs a random
        one rather than carrying on with it -- the autoresponder ACKs whatever is in
        these two registers, so an address the chip cannot own makes it answer nothing.
        """
        if _is_valid_ether_addr(addr):
            self.macaddr = bytes(addr)
        else:
            self.macaddr = _random_ether_addr()
            logger.info("Invalid MAC address, using random address %s",
                        ":".join(f"{b:02x}" for b in self.macaddr))
        self.tp.wr(MT_MAC_ADDR_DW0, int.from_bytes(self.macaddr[0:4], "little"))
        self.tp.wr(MT_MAC_ADDR_DW1,
                   int.from_bytes(self.macaddr[4:6], "little")
                   | _field_prep(MT_MAC_ADDR_DW1_U2ME_MASK, 0xFF))

    def has_tssi(self, eeprom: bytes) -> bool:
        """eeprom.c:98 -- TSSI needs a non-all-ones NIC_CONF_1 with TX ALC enabled."""
        nic_conf1 = int.from_bytes(eeprom[MT_EE_NIC_CONF_1:MT_EE_NIC_CONF_1 + 2], "little")
        return ((~nic_conf1) & 0xFFFF) != 0 and bool(nic_conf1 & MT_EE_NIC_CONF_1_TX_ALC_EN)

    def set_chip_cap(self, eeprom: bytes) -> None:
        """eeprom.c:106 -- decide TSSI mode and flag hardware this driver can't drive."""
        nic_conf0 = int.from_bytes(eeprom[MT_EE_NIC_CONF_0:MT_EE_NIC_CONF_0 + 2], "little")
        nic_conf1 = int.from_bytes(eeprom[MT_EE_NIC_CONF_1:MT_EE_NIC_CONF_1 + 2], "little")
        if not field_valid(nic_conf1 & 0xFF):
            nic_conf1 &= 0xFF00

        self.ee.tssi_enabled = self.has_tssi(eeprom) and not (nic_conf1 & MT_EE_NIC_CONF_1_TEMP_TX_ALC)

        if nic_conf1 & MT_EE_NIC_CONF_1_HW_RF_CTRL:
            logger.error("Error: this driver does not support HW RF ctrl")

        if not field_valid(nic_conf0 >> 8):
            return
        if (_field_get(MT_EE_NIC_CONF_0_RX_PATH, nic_conf0) > 1
                or _field_get(MT_EE_NIC_CONF_0_TX_PATH, nic_conf0) > 1):
            logger.error("Error: device has more than 1 RX/TX stream!")

    def set_channel_power(self, eeprom: bytes) -> None:
        """eeprom.c:145 -- per-channel TX power, from the ALC limit or the EEPROM table."""
        val = self.tp.rr(MT_TX_ALC_CFG_0)
        max_pwr = _field_get(MT_TX_ALC_CFG_0_LIMIT_0, val)

        if self.has_tssi(eeprom):
            self._set_channel_target_power(eeprom, max_pwr)
            return

        for i in range(N_CHAN_PWR):
            power = _s8(field_validate(eeprom[MT_EE_TX_POWER_OFFSET + i]))
            if power > max_pwr or power < 0:
                power = MT7601U_DEFAULT_TX_POWER
            self.ee.chan_pwr[i] = power

    def _set_channel_target_power(self, eeprom: bytes, max_pwr: int) -> None:
        """eeprom.c:130 -- TSSI cards use one target power for every channel."""
        trgt_pwr = eeprom[MT_EE_TX_TSSI_TARGET_POWER]
        if trgt_pwr > max_pwr or not trgt_pwr:
            logger.warning("Error: EEPROM trgt power invalid %02x!", trgt_pwr)
            trgt_pwr = _DEFAULT_TARGET_PWR
        self.ee.chan_pwr = [trgt_pwr] * N_CHAN_PWR

    def set_country_reg(self, eeprom: bytes) -> None:
        """eeprom.c:169 -- map the EEPROM's country region onto a channel plan.

        Region 31 is not valid for this part (eeprom.c:171), and anything the table
        doesn't recognise falls back to channels 1-14.
        """
        chan_bounds = [
            RegChannelBounds(1, 11), RegChannelBounds(1, 13),
            RegChannelBounds(10, 2), RegChannelBounds(10, 4),
            RegChannelBounds(14, 1), RegChannelBounds(1, 14),
            RegChannelBounds(3, 7), RegChannelBounds(5, 9),
            # EEPROM country regions 32-33
            RegChannelBounds(1, 11), RegChannelBounds(1, 14),
        ]
        val = eeprom[MT_EE_COUNTRY_REGION]
        idx = -1
        if val < 8:
            idx = val
        if 31 < val < 33:
            idx = val - 32 + 8

        if idx != -1:
            logger.info("EEPROM country region %02x (channels %d-%d)", val,
                        chan_bounds[idx].start,
                        chan_bounds[idx].start + chan_bounds[idx].num - 1)
        else:
            idx = 5                      # channels 1-14

        self.ee.reg = chan_bounds[idx]

    def set_rf_freq_off(self, eeprom: bytes) -> None:
        """eeprom.c:205 -- EEPROM frequency offset plus its signed compensation."""
        comp = field_validate(eeprom[MT_EE_FREQ_OFFSET_COMPENSATION])
        self.ee.rf_freq_off = field_validate(eeprom[MT_EE_FREQ_OFFSET])
        if comp & 0x80:
            self.ee.rf_freq_off -= comp & 0x7F
        else:
            self.ee.rf_freq_off += comp

    def set_rssi_offset(self, eeprom: bytes) -> None:
        """eeprom.c:219 -- RSSI offsets outside +/-10 are nonsense; zero them."""
        for i in range(2):
            offset = _s8(eeprom[MT_EE_RSSI_OFFSET + i])
            if not -10 <= offset <= 10:
                logger.warning("Warning: EEPROM RSSI is invalid %02x", offset)
                offset = 0
            self.ee.rssi_offset[i] = offset

    def _set_power_rate(self, rate: PowerPerRate, delta: int, value: int) -> None:
        """eeprom.c:250 -- 0xff means "leave the default alone" (eeprom.c:253)."""
        if value == _INVALID_FIELD:
            return
        rate.raw = s6_validate(value)
        rate.bw20 = s6_to_int(value)
        rate.bw40 = rate.bw20 + delta

    def _save_power_rate(self, delta: int, val: int, i: int) -> None:
        """eeprom.c:263 -- unpack one 32-bit EEPROM word into two rates."""
        t = self.ee.power_rate_table
        if i == 0:
            self._set_power_rate(t.cck[0], delta, (val >> 0) & 0xFF)
            self._set_power_rate(t.cck[1], delta, (val >> 8) & 0xFF)
            # Save cck bw20 for fixups of channel 14 (eeprom.c:271)
            self.ee.real_cck_bw20[0] = t.cck[0].bw20
            self.ee.real_cck_bw20[1] = t.cck[1].bw20
            self._set_power_rate(t.ofdm[0], delta, (val >> 16) & 0xFF)
            self._set_power_rate(t.ofdm[1], delta, (val >> 24) & 0xFF)
        elif i == 1:
            self._set_power_rate(t.ofdm[2], delta, (val >> 0) & 0xFF)
            self._set_power_rate(t.ofdm[3], delta, (val >> 8) & 0xFF)
            self._set_power_rate(t.ht[0], delta, (val >> 16) & 0xFF)
            self._set_power_rate(t.ht[1], delta, (val >> 24) & 0xFF)
        elif i == 2:
            self._set_power_rate(t.ht[2], delta, (val >> 0) & 0xFF)
            self._set_power_rate(t.ht[3], delta, (val >> 8) & 0xFF)

    def config_tx_power_per_rate(self, eeprom: bytes) -> None:
        """eeprom.c:309 -- five EEPROM words, each carrying four per-rate powers."""
        bw40_delta = self._get_delta(eeprom[MT_EE_TX_POWER_DELTA_BW40])
        for i in range(N_RATE_POWER_GROUPS):
            val = int.from_bytes(eeprom[MT_EE_TX_POWER_BYRATE(i):MT_EE_TX_POWER_BYRATE(i) + 4], "little")
            self._save_power_rate(bw40_delta, val, i)
            if ~val & 0xFFFFFFFF:
                self.tp.wr(MT_TX_PWR_CFG_0 + i * 4, val)
        self._extra_power_over_mac()

    @staticmethod
    def _get_delta(val: int) -> int:
        """eeprom.c:292 -- a signed 3-bit delta, clamped to +/-8."""
        if not field_valid(val) or not (val & 0x80):
            return 0
        ret = val & 0x1F
        if ret > 8:
            ret = 8
        return -ret if val & 0x40 else ret

    def _extra_power_over_mac(self) -> None:
        """eeprom.c:237 -- fold the per-rate power deltas back into the MAC registers."""
        val = (self.tp.rr(MT_TX_PWR_CFG_1) & 0x0000FF00) >> 8
        val |= (self.tp.rr(MT_TX_PWR_CFG_2) & 0x0000FF00) << 8
        self.tp.wr(MT_TX_PWR_CFG_7, val)
        self.tp.wr(MT_TX_PWR_CFG_9, (self.tp.rr(MT_TX_PWR_CFG_4) & 0x0000FF00) >> 8)

    def init_tssi_params(self, eeprom: bytes) -> None:
        """eeprom.c:330 -- only populated when TSSI is enabled."""
        if not self.ee.tssi_enabled:
            return
        d = self.ee.tssi_data
        d.slope = eeprom[MT_EE_TX_TSSI_SLOPE]
        d.tx0_delta_offset = eeprom[MT_EE_TX_TSSI_OFFSET] * 1024
        d.offset[0] = eeprom[MT_EE_TX_TSSI_OFFSET_GROUP]
        d.offset[1] = eeprom[MT_EE_TX_TSSI_OFFSET_GROUP + 1]
        d.offset[2] = eeprom[MT_EE_TX_TSSI_OFFSET_GROUP + 2]

    # ------------------------------------------------------------------
    # Entry point (eeprom.c:344 mt7601u_eeprom_init)
    # ------------------------------------------------------------------

    def physical_size_check(self) -> None:
        """eeprom.c:67 -- refuse a card whose usage map needs a default EEPROM file."""
        data = bytearray()
        for i in range(_MAP_READS):
            data += self.efuse_read(MT_EE_USAGE_MAP_START + i * 16, MT_EE_PHYSICAL_READ)

        start, end, cnt_free = 0, 0, 0
        for i in range(MT_EFUSE_USAGE_MAP_SIZE):
            if not data[i]:
                if not start:
                    start = MT_EE_USAGE_MAP_START + i
                end = MT_EE_USAGE_MAP_START + i
        cnt_free = end - start + 1

        if MT_EFUSE_USAGE_MAP_SIZE - cnt_free < 5:
            raise EepromError("your device needs a default EEPROM file "
                              "and this driver doesn't support it!")

    def read(self) -> MT7601UEepromParams:
        """Read and decode the whole EEPROM. Mirrors mt7601u_eeprom_init.

        The result is filled into ``self.ee`` in place, not a new object: the phy
        module holds the reference it was constructed with, and rebinding would
        leave the TX power path reading an empty table.
        """
        self.physical_size_check()

        eeprom = bytearray(MT7601U_EEPROM_SIZE)
        for i in range(0, MT7601U_EEPROM_SIZE, 16):
            eeprom[i:i + 16] = self.efuse_read(i, MT_EE_READ)
        self.raw = eeprom

        if eeprom[MT_EE_VERSION_EE] > MT7601U_EE_MAX_VER:
            logger.warning("Warning: unsupported EEPROM version %02x", eeprom[MT_EE_VERSION_EE])
        self.version_ee = eeprom[MT_EE_VERSION_EE]
        self.version_fae = eeprom[MT_EE_VERSION_FAE]
        logger.info("EEPROM ver:%02x fae:%02x", self.version_ee, self.version_fae)

        self.set_macaddr(bytes(eeprom[MT_EE_MAC_ADDR:MT_EE_MAC_ADDR + 6]))
        self.set_chip_cap(eeprom)
        self.set_channel_power(eeprom)
        self.set_country_reg(eeprom)
        self.set_rf_freq_off(eeprom)
        self.set_rssi_offset(eeprom)
        self.ee.ref_temp = _s8(eeprom[MT_EE_REF_TEMP])
        self.ee.lna_gain = _s8(eeprom[MT_EE_LNA_GAIN])

        self.config_tx_power_per_rate(eeprom)
        self.init_tssi_params(eeprom)
        return self.ee