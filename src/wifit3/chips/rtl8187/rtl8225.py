"""RTL8225/RTL8225z2 RF init + SPI primitives.

Ported from ``driver_sources/rtl818x-source-v6.18/rtl8187/rtl8225.c``.

The RTL8187L's transceiver is an external RF chip (the "Realtek RTL8225",
later revved to "RTL8225z2" — the BCD vs. z2 split is what
``rtl8187_detect_rf`` discriminates between via an SPI probe). The MAC
talks to it through 4 SPI lines wired to GPIO bits in
``RFPinsOutput``/``RFPinsSelect``/``RFPinsEnable``/``RFPinsInput``.

M2b implements:
  * SPI primitives (``rtl8225_write_bitbang``, ``rtl8225_write_8051``,
    ``rtl8225_read``)
  * PHY register helpers (``rtl8187_write_phy`` + ofdm/cck variants)
  * ``rtl8187_detect_rf`` (asic_rev probe + RF reg-0/8/9 read to choose
    rtl8225 BCD vs rtl8225z2)
  * Full ``rtl8225_rf_init`` (BCD variant only — z2 is M2c if needed)
  * Tables: ``rtl8225bcd_rxgain``, ``rtl8225_agc``, ``rtl8225_gain``,
    ``rtl8225_threshold``, ``rtl8225_tx_gain_cck_ofdm``,
    ``rtl8225_tx_power_cck``, ``rtl8225_tx_power_cck_ch14``,
    ``rtl8225_tx_power_ofdm``, ``rtl8225_chan``

TX power readouts that the kernel pulls from the 93cx6 EEPROM
(``priv->channels[ch-1].hw_value``) are stubbed with conservative zeros
for M2b — RX comes up fine without accurate TX power, and we defer
EEPROM bit-banging until later (see [[feedback_defer_efuse_on_bring_up]]).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from .constants import (
    ANAPARAM2_ON,
    CONFIG3_ANAPARAM_WRITE,
    EEPROM_CMD_CONFIG,
    EEPROM_CMD_NORMAL,
    REG_ANAPARAM2,
    REG_CONFIG3,
    REG_EEPROM_CMD,
    REG_PHY,
    REG_RFPINSENABLE,
    REG_RFPINSINPUT,
    REG_RFPINSOUTPUT,
    REG_RFPINSSELECT,
    REG_TESTR,
    REG_TX_ANTENNA,
    REG_TX_GAIN_CCK,
    REG_TX_GAIN_OFDM,
)
from .transport import RTL8187Transport

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# RF variant — what ``rtl8187_detect_rf`` returns.
# ----------------------------------------------------------------------
class RfVariant(str, Enum):
    RTL8225 = "rtl8225"      # BCD revision (M2b ported)
    RTL8225Z2 = "rtl8225z2"  # z2 revision (M2c — not yet)


# ----------------------------------------------------------------------
# Per-channel TX power read from the 93cx6 EEPROM during probe.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class TxPower:
    """The kernel reads ``priv->channels[ch-1].hw_value`` (cck in the low nibble, ofdm in
    the high nibble) and ``priv->txpwr_base`` from the EEPROM, and feeds them to every
    ``set_tx_power`` (RF init for channel 1, then each channel tune). hw_value has 14
    entries (channels 1..14); ``base`` is added by the z2 setter only. [SRC] dev.c probe +
    rtl8225{,z2}_rf_set_tx_power."""

    hw_value: tuple[int, ...]
    base: int = 0


def set_tx_power(t: "RTL8187Transport", variant: "RfVariant", channel: int,
                 power: TxPower) -> None:
    """Dispatch the per-variant TX-power refresh for ``channel`` using the EEPROM table.

    cck_power = hw_value & 0xF, ofdm_power = hw_value >> 4 (the kernel's extraction at the
    top of rtl8225{,z2}_rf_set_tx_power). BCD ignores ``base``; z2 folds it in."""
    hv = power.hw_value[channel - 1]
    cck_power, ofdm_power = hv & 0xF, hv >> 4
    if variant is RfVariant.RTL8225Z2:
        rtl8225z2_rf_set_tx_power(t, channel, cck_power, ofdm_power, power.base)
    else:
        rtl8225_rf_set_tx_power(t, channel, cck_power, ofdm_power)


# ----------------------------------------------------------------------
# busy-wait helper — kernel uses udelay(1..10). Python time.sleep below
# ~1ms is unreliable on Windows, but for SPI bit-bang we don't strictly
# need each delay to be exact — the chip is forgiving. We do a coarse
# perf_counter spin which is good enough.
# ----------------------------------------------------------------------
def _udelay(us: float) -> None:
    if us <= 0:
        return
    deadline = time.perf_counter() + us / 1_000_000.0
    while time.perf_counter() < deadline:
        pass


# ----------------------------------------------------------------------
# PHY helpers (CCK + OFDM share rtl8187_write_phy)
# [SRC] dev.c:173-184, rtl8225.h:29-39
# ----------------------------------------------------------------------
def write_phy(t: RTL8187Transport, addr: int, data: int) -> None:
    """rtl8187_write_phy — 4-byte sequential write to REG_PHY[0..3].

    Wire layout (kernel):
        data <<= 8
        data |= addr | 0x80
        PHY[3] = data[31:24]
        PHY[2] = data[23:16]
        PHY[1] = data[15:8]
        PHY[0] = data[7:0]
    """
    data = (data << 8) | (addr & 0xFF) | 0x80
    t.write8(REG_PHY + 3, (data >> 24) & 0xFF)
    t.write8(REG_PHY + 2, (data >> 16) & 0xFF)
    t.write8(REG_PHY + 1, (data >> 8) & 0xFF)
    t.write8(REG_PHY + 0, data & 0xFF)


def write_phy_ofdm(t: RTL8187Transport, addr: int, data: int) -> None:
    write_phy(t, addr, data)


def write_phy_cck(t: RTL8187Transport, addr: int, data: int) -> None:
    # CCK regs share the same PHY bus — bit 16 of data is the CCK select.
    write_phy(t, addr, data | 0x10000)


# asic_rev (the SPI-path selector, low 2 bits of 0xFFFE under PGSELECT|1) is read in
# :func:`wifit3.chips.rtl8187.probe.probe`, inside the probe's single EEPROM_CMD window
# — there is no standalone asic_rev probe, so it cannot drift from the kernel sequence.


# ----------------------------------------------------------------------
# SPI write — bitbang variant (asic_rev == 0)
# [SRC] rtl8225.c:115-156
# ----------------------------------------------------------------------
def rtl8225_write_bitbang(t: RTL8187Transport, addr: int, data: int) -> None:
    bangdata = (data << 4) | (addr & 0xF)

    reg80 = t.read16(REG_RFPINSOUTPUT) & 0xFFF3
    reg82 = t.read16(REG_RFPINSENABLE)

    t.write16(REG_RFPINSENABLE, reg82 | 0x7)

    reg84 = t.read16(REG_RFPINSSELECT)
    t.write16(REG_RFPINSSELECT, reg84 | 0x7)
    _udelay(10)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    _udelay(2)
    t.write16(REG_RFPINSOUTPUT, reg80)
    _udelay(10)

    for i in range(15, -1, -1):
        reg = reg80 | ((bangdata & (1 << i)) >> i)

        if i & 1:
            t.write16(REG_RFPINSOUTPUT, reg)

        t.write16(REG_RFPINSOUTPUT, reg | (1 << 1))
        t.write16(REG_RFPINSOUTPUT, reg | (1 << 1))

        if not (i & 1):
            t.write16(REG_RFPINSOUTPUT, reg)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    _udelay(10)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    t.write16(REG_RFPINSSELECT, reg84)


# ----------------------------------------------------------------------
# SPI write — 8051 fast path (asic_rev != 0)
# [SRC] rtl8225.c:158-195
# ----------------------------------------------------------------------
def rtl8225_write_8051(t: RTL8187Transport, addr: int, data: int) -> None:
    reg80 = t.read16(REG_RFPINSOUTPUT)
    reg82 = t.read16(REG_RFPINSENABLE)
    reg84 = t.read16(REG_RFPINSSELECT)

    reg80 &= ~(0x3 << 2)
    reg84 &= ~0xF

    t.write16(REG_RFPINSENABLE, reg82 | 0x0007)
    t.write16(REG_RFPINSSELECT, reg84 | 0x0007)
    _udelay(10)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    _udelay(2)

    t.write16(REG_RFPINSOUTPUT, reg80)
    _udelay(10)

    # Magic dual-arg control transfer: wValue=addr, wIndex=0x8225.
    # Kernel goes through usb_control_msg directly — payload is the
    # little-endian 16-bit data.
    payload = [data & 0xFF, (data >> 8) & 0xFF]
    t.dev.ctrl_transfer(
        0x40,           # vendor OUT
        0x05,           # RTL8187_REQ_SET_REG
        addr,           # wValue = RF reg addr
        0x8225,         # wIndex = magic 8225 marker
        payload,
        500,
    )

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    _udelay(10)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    t.write16(REG_RFPINSSELECT, reg84)


def rtl8225_write(t: RTL8187Transport, addr: int, data: int, asic_rev: int) -> None:
    """Dispatch to bitbang or 8051 path based on asic_rev.  [SRC] rtl8225.c:197-205"""
    if asic_rev:
        rtl8225_write_8051(t, addr, data)
    else:
        rtl8225_write_bitbang(t, addr, data)


# ----------------------------------------------------------------------
# SPI read (always uses bitbang protocol — no 8051 fast path)
# [SRC] rtl8225.c:207-290
# ----------------------------------------------------------------------
def rtl8225_read(t: RTL8187Transport, addr: int) -> int:
    reg80 = t.read16(REG_RFPINSOUTPUT)
    reg82 = t.read16(REG_RFPINSENABLE)
    reg84 = t.read16(REG_RFPINSSELECT)

    reg80 &= ~0xF

    t.write16(REG_RFPINSENABLE, reg82 | 0x000F)
    t.write16(REG_RFPINSSELECT, reg84 | 0x000F)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 2))
    _udelay(4)
    t.write16(REG_RFPINSOUTPUT, reg80)
    _udelay(5)

    for i in range(4, -1, -1):
        reg = reg80 | ((addr >> i) & 1)

        if not (i & 1):
            t.write16(REG_RFPINSOUTPUT, reg)
            _udelay(1)

        t.write16(REG_RFPINSOUTPUT, reg | (1 << 1))
        _udelay(2)
        t.write16(REG_RFPINSOUTPUT, reg | (1 << 1))
        _udelay(2)

        if i & 1:
            t.write16(REG_RFPINSOUTPUT, reg)
            _udelay(1)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3) | (1 << 1))
    _udelay(2)
    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3))
    _udelay(2)
    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3))
    _udelay(2)

    out = 0
    for i in range(11, -1, -1):
        t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3))
        _udelay(1)
        t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3) | (1 << 1))
        _udelay(2)
        t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3) | (1 << 1))
        _udelay(2)
        t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3) | (1 << 1))
        _udelay(2)

        if t.read16(REG_RFPINSINPUT) & (1 << 1):
            out |= 1 << i

        t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3))
        _udelay(2)

    t.write16(REG_RFPINSOUTPUT, reg80 | (1 << 3) | (1 << 2))
    _udelay(2)

    t.write16(REG_RFPINSENABLE, reg82)
    t.write16(REG_RFPINSSELECT, reg84)
    t.write16(REG_RFPINSOUTPUT, 0x03A0)

    return out


# ----------------------------------------------------------------------
# detect_rf — picks BCD vs z2 by reading RF regs 8 & 9.
# [SRC] rtl8225.c:1025-1044
# ----------------------------------------------------------------------
def detect_rf(t: RTL8187Transport, asic_rev: int) -> RfVariant:
    """Probe the external RF chip to discriminate BCD vs z2.

    The kernel writes RF reg 0 = 0x1B7, then reads RF regs 8 and 9. z2
    silicon reads back 0x588 / 0x700; anything else is BCD.
    """
    rtl8225_write(t, 0, 0x1B7, asic_rev)
    reg8 = rtl8225_read(t, 8)
    reg9 = rtl8225_read(t, 9)
    rtl8225_write(t, 0, 0x0B7, asic_rev)

    if reg8 == 0x588 and reg9 == 0x700:
        logger.debug("RF detect: reg8=0x%x reg9=0x%x → RTL8225z2", reg8, reg9)
        return RfVariant.RTL8225Z2
    logger.debug("RF detect: reg8=0x%x reg9=0x%x → RTL8225 (BCD)", reg8, reg9)
    return RfVariant.RTL8225


# ----------------------------------------------------------------------
# Tables (1:1 from rtl8225.c)
# ----------------------------------------------------------------------
# [SRC] rtl8225.c:292-305
rtl8225bcd_rxgain = (
    0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0408, 0x0409,
    0x040a, 0x040b, 0x0502, 0x0503, 0x0504, 0x0505, 0x0540, 0x0541,
    0x0542, 0x0543, 0x0544, 0x0545, 0x0580, 0x0581, 0x0582, 0x0583,
    0x0584, 0x0585, 0x0588, 0x0589, 0x058a, 0x058b, 0x0643, 0x0644,
    0x0645, 0x0680, 0x0681, 0x0682, 0x0683, 0x0684, 0x0685, 0x0688,
    0x0689, 0x068a, 0x068b, 0x068c, 0x0742, 0x0743, 0x0744, 0x0745,
    0x0780, 0x0781, 0x0782, 0x0783, 0x0784, 0x0785, 0x0788, 0x0789,
    0x078a, 0x078b, 0x078c, 0x078d, 0x0790, 0x0791, 0x0792, 0x0793,
    0x0794, 0x0795, 0x0798, 0x0799, 0x079a, 0x079b, 0x079c, 0x079d,
    0x07a0, 0x07a1, 0x07a2, 0x07a3, 0x07a4, 0x07a5, 0x07a8, 0x07a9,
    0x07aa, 0x07ab, 0x07ac, 0x07ad, 0x07b0, 0x07b1, 0x07b2, 0x07b3,
    0x07b4, 0x07b5, 0x07b8, 0x07b9, 0x07ba, 0x07bb, 0x07bb,
)

# [SRC] rtl8225.c:307-324
rtl8225_agc = (
    0x9e, 0x9e, 0x9e, 0x9e, 0x9e, 0x9e, 0x9e, 0x9e,
    0x9d, 0x9c, 0x9b, 0x9a, 0x99, 0x98, 0x97, 0x96,
    0x95, 0x94, 0x93, 0x92, 0x91, 0x90, 0x8f, 0x8e,
    0x8d, 0x8c, 0x8b, 0x8a, 0x89, 0x88, 0x87, 0x86,
    0x85, 0x84, 0x83, 0x82, 0x81, 0x80, 0x3f, 0x3e,
    0x3d, 0x3c, 0x3b, 0x3a, 0x39, 0x38, 0x37, 0x36,
    0x35, 0x34, 0x33, 0x32, 0x31, 0x30, 0x2f, 0x2e,
    0x2d, 0x2c, 0x2b, 0x2a, 0x29, 0x28, 0x27, 0x26,
    0x25, 0x24, 0x23, 0x22, 0x21, 0x20, 0x1f, 0x1e,
    0x1d, 0x1c, 0x1b, 0x1a, 0x19, 0x18, 0x17, 0x16,
    0x15, 0x14, 0x13, 0x12, 0x11, 0x10, 0x0f, 0x0e,
    0x0d, 0x0c, 0x0b, 0x0a, 0x09, 0x08, 0x07, 0x06,
    0x05, 0x04, 0x03, 0x02, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
)

# [SRC] rtl8225.c:326-334  (7 sensitivity rows × 4 entries — { ofdm_a,b,c,d })
rtl8225_gain = (
    0x23, 0x88, 0x7c, 0xa5,   # -82dBm
    0x23, 0x88, 0x7c, 0xb5,   # -82dBm
    0x23, 0x88, 0x7c, 0xc5,   # -82dBm
    0x33, 0x80, 0x79, 0xc5,   # -78dBm
    0x43, 0x78, 0x76, 0xc5,   # -74dBm
    0x53, 0x60, 0x73, 0xc5,   # -70dBm
    0x63, 0x58, 0x70, 0xc5,   # -66dBm
)

# [SRC] rtl8225.c:336-338  (CCK threshold per sensitivity row)
rtl8225_threshold = (0x8d, 0x8d, 0x8d, 0x8d, 0x9d, 0xad, 0xbd)

# [SRC] rtl8225.c:340-342
rtl8225_tx_gain_cck_ofdm = (0x02, 0x06, 0x0e, 0x1e, 0x3e, 0x7e)

# [SRC] rtl8225.c:344-351  (6 × 8 = 48; per-cck-power × 8 sub-entries)
rtl8225_tx_power_cck = (
    0x18, 0x17, 0x15, 0x11, 0x0c, 0x08, 0x04, 0x02,
    0x1b, 0x1a, 0x17, 0x13, 0x0e, 0x09, 0x04, 0x02,
    0x1f, 0x1e, 0x1a, 0x15, 0x10, 0x0a, 0x05, 0x02,
    0x22, 0x21, 0x1d, 0x18, 0x11, 0x0b, 0x06, 0x02,
    0x26, 0x25, 0x21, 0x1b, 0x14, 0x0d, 0x06, 0x03,
    0x2b, 0x2a, 0x25, 0x1e, 0x16, 0x0e, 0x07, 0x03,
)

# [SRC] rtl8225.c:353-360
rtl8225_tx_power_cck_ch14 = (
    0x18, 0x17, 0x15, 0x0c, 0x00, 0x00, 0x00, 0x00,
    0x1b, 0x1a, 0x17, 0x0e, 0x00, 0x00, 0x00, 0x00,
    0x1f, 0x1e, 0x1a, 0x0f, 0x00, 0x00, 0x00, 0x00,
    0x22, 0x21, 0x1d, 0x11, 0x00, 0x00, 0x00, 0x00,
    0x26, 0x25, 0x21, 0x13, 0x00, 0x00, 0x00, 0x00,
    0x2b, 0x2a, 0x25, 0x15, 0x00, 0x00, 0x00, 0x00,
)

# [SRC] rtl8225.c:362-364
rtl8225_tx_power_ofdm = (0x80, 0x90, 0xa2, 0xb5, 0xcb, 0xe4)

# [SRC] rtl8225.c:366-369  (per-channel synthesizer word for chans 1..14)
rtl8225_chan = (
    0x085c, 0x08dc, 0x095c, 0x09dc, 0x0a5c, 0x0adc, 0x0b5c,
    0x0bdc, 0x0c5c, 0x0cdc, 0x0d5c, 0x0ddc, 0x0e5c, 0x0f72,
)


# ----------------------------------------------------------------------
# rtl8225_rf_set_tx_power — sets TX power for a given channel.
# [SRC] rtl8225.c:371-425
#
# Kernel reads cck_power / ofdm_power from priv->channels[ch-1].hw_value
# which originates in the 93cx6 EEPROM. We defer EEPROM bit-banging
# until later (see [[feedback_defer_efuse_on_bring_up]]) and accept a
# stub-callable for the power lookup; default is (0, 0) which works
# fine for RX-only bring-up.
# ----------------------------------------------------------------------
def rtl8225_rf_set_tx_power(
    t: RTL8187Transport,
    channel: int,
    cck_power: int = 0,
    ofdm_power: int = 0,
) -> None:
    cck_power = min(cck_power, 11)
    if ofdm_power > 15:
        ofdm_power = 25
    else:
        ofdm_power += 10

    t.write8(REG_TX_GAIN_CCK, rtl8225_tx_gain_cck_ofdm[cck_power // 6] >> 1)

    if channel == 14:
        tbl = rtl8225_tx_power_cck_ch14
    else:
        tbl = rtl8225_tx_power_cck
    base = (cck_power % 6) * 8
    for i in range(8):
        write_phy_cck(t, 0x44 + i, tbl[base + i])

    time.sleep(0.001)

    # ANAPARAM2 ON window (matches kernel set_anaparam's bracket).
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG3)
    t.write8(REG_CONFIG3, reg | CONFIG3_ANAPARAM_WRITE)
    t.write32(REG_ANAPARAM2, ANAPARAM2_ON)
    t.write8(REG_CONFIG3, reg & ~CONFIG3_ANAPARAM_WRITE & 0xFF)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)

    write_phy_ofdm(t, 2, 0x42)
    write_phy_ofdm(t, 6, 0x00)
    write_phy_ofdm(t, 8, 0x00)

    t.write8(REG_TX_GAIN_OFDM, rtl8225_tx_gain_cck_ofdm[ofdm_power // 6] >> 1)
    ofdm_val = rtl8225_tx_power_ofdm[ofdm_power % 6]
    write_phy_ofdm(t, 5, ofdm_val)
    write_phy_ofdm(t, 7, ofdm_val)

    time.sleep(0.001)


# ----------------------------------------------------------------------
# rtl8225_rf_init — full BCD-revision RF bring-up.
# [SRC] rtl8225.c:427-569
# ----------------------------------------------------------------------
def rtl8225_rf_init(t: RTL8187Transport, asic_rev: int, power: TxPower) -> None:
    """Port of rtl8225_rf_init (rtl8225.c:427-569).

    Brings the external transceiver up: writes the canonical 16-entry
    RF register init sequence, retries RF calibration up to twice if
    the cal-OK bit (RF reg 6 bit 7) doesn't come up, then loads
    rxgain/agc/ofdm/cck/phy tables, sets the channel-1 TX power (from the
    EEPROM ``power`` table), and tunes RX-A antenna sensitivity.

    asic_rev selects bitbang vs 8051 SPI; pass the value from
    :func:`probe_asic_rev`.
    """
    def rfw(addr: int, data: int) -> None:
        rtl8225_write(t, addr, data, asic_rev)

    # 16-entry canonical RF init sequence.
    rfw(0x0, 0x067)
    rfw(0x1, 0xFE0)
    rfw(0x2, 0x44D)
    rfw(0x3, 0x441)
    rfw(0x4, 0x486)
    rfw(0x5, 0xBC0)
    rfw(0x6, 0xAE6)
    rfw(0x7, 0x82A)
    rfw(0x8, 0x01F)
    rfw(0x9, 0x334)
    rfw(0xA, 0xFD4)
    rfw(0xB, 0x391)
    rfw(0xC, 0x050)
    rfw(0xD, 0x6DB)
    rfw(0xE, 0x029)
    rfw(0xF, 0x914)
    time.sleep(0.100)

    rfw(0x2, 0xC4D)
    time.sleep(0.200)
    rfw(0x2, 0x44D)
    time.sleep(0.200)

    # RF calibration check — try once more if it didn't latch.
    if not (rtl8225_read(t, 6) & (1 << 7)):
        rfw(0x02, 0x0c4d)
        time.sleep(0.200)
        rfw(0x02, 0x044d)
        time.sleep(0.100)
        if not (rtl8225_read(t, 6) & (1 << 7)):
            logger.warning(
                "rtl8225: RF calibration failed, reg6=0x%x", rtl8225_read(t, 6)
            )

    rfw(0x0, 0x127)

    for i, val in enumerate(rtl8225bcd_rxgain):
        rfw(0x1, i + 1)
        rfw(0x2, val)

    rfw(0x0, 0x027)
    rfw(0x0, 0x22F)

    for i, val in enumerate(rtl8225_agc):
        write_phy_ofdm(t, 0xB, val)
        write_phy_ofdm(t, 0xA, 0x80 + i)

    time.sleep(0.001)

    # OFDM PHY register init (38 writes).
    write_phy_ofdm(t, 0x00, 0x01)
    write_phy_ofdm(t, 0x01, 0x02)
    write_phy_ofdm(t, 0x02, 0x42)
    write_phy_ofdm(t, 0x03, 0x00)
    write_phy_ofdm(t, 0x04, 0x00)
    write_phy_ofdm(t, 0x05, 0x00)
    write_phy_ofdm(t, 0x06, 0x40)
    write_phy_ofdm(t, 0x07, 0x00)
    write_phy_ofdm(t, 0x08, 0x40)
    write_phy_ofdm(t, 0x09, 0xfe)
    write_phy_ofdm(t, 0x0a, 0x09)
    write_phy_ofdm(t, 0x0b, 0x80)
    write_phy_ofdm(t, 0x0c, 0x01)
    write_phy_ofdm(t, 0x0e, 0xd3)
    write_phy_ofdm(t, 0x0f, 0x38)
    write_phy_ofdm(t, 0x10, 0x84)
    write_phy_ofdm(t, 0x11, 0x06)
    write_phy_ofdm(t, 0x12, 0x20)
    write_phy_ofdm(t, 0x13, 0x20)
    write_phy_ofdm(t, 0x14, 0x00)
    write_phy_ofdm(t, 0x15, 0x40)
    write_phy_ofdm(t, 0x16, 0x00)
    write_phy_ofdm(t, 0x17, 0x40)
    write_phy_ofdm(t, 0x18, 0xef)
    write_phy_ofdm(t, 0x19, 0x19)
    write_phy_ofdm(t, 0x1a, 0x20)
    write_phy_ofdm(t, 0x1b, 0x76)
    write_phy_ofdm(t, 0x1c, 0x04)
    write_phy_ofdm(t, 0x1e, 0x95)
    write_phy_ofdm(t, 0x1f, 0x75)
    write_phy_ofdm(t, 0x20, 0x1f)
    write_phy_ofdm(t, 0x21, 0x27)
    write_phy_ofdm(t, 0x22, 0x16)
    write_phy_ofdm(t, 0x24, 0x46)
    write_phy_ofdm(t, 0x25, 0x20)
    write_phy_ofdm(t, 0x26, 0x90)
    write_phy_ofdm(t, 0x27, 0x88)

    # Sensitivity row 2 (-82dBm).
    write_phy_ofdm(t, 0x0d, rtl8225_gain[2 * 4 + 0])
    write_phy_ofdm(t, 0x1b, rtl8225_gain[2 * 4 + 2])
    write_phy_ofdm(t, 0x1d, rtl8225_gain[2 * 4 + 3])
    write_phy_ofdm(t, 0x23, rtl8225_gain[2 * 4 + 1])

    # CCK PHY register init (28 writes).
    write_phy_cck(t, 0x00, 0x98)
    write_phy_cck(t, 0x03, 0x20)
    write_phy_cck(t, 0x04, 0x7e)
    write_phy_cck(t, 0x05, 0x12)
    write_phy_cck(t, 0x06, 0xfc)
    write_phy_cck(t, 0x07, 0x78)
    write_phy_cck(t, 0x08, 0x2e)
    write_phy_cck(t, 0x10, 0x9b)
    write_phy_cck(t, 0x11, 0x88)
    write_phy_cck(t, 0x12, 0x47)
    write_phy_cck(t, 0x13, 0xd0)
    write_phy_cck(t, 0x19, 0x00)
    write_phy_cck(t, 0x1a, 0xa0)
    write_phy_cck(t, 0x1b, 0x08)
    write_phy_cck(t, 0x40, 0x86)
    write_phy_cck(t, 0x41, 0x8d)
    write_phy_cck(t, 0x42, 0x15)
    write_phy_cck(t, 0x43, 0x18)
    write_phy_cck(t, 0x44, 0x1f)
    write_phy_cck(t, 0x45, 0x1e)
    write_phy_cck(t, 0x46, 0x1a)
    write_phy_cck(t, 0x47, 0x15)
    write_phy_cck(t, 0x48, 0x10)
    write_phy_cck(t, 0x49, 0x0a)
    write_phy_cck(t, 0x4a, 0x05)
    write_phy_cck(t, 0x4b, 0x02)
    write_phy_cck(t, 0x4c, 0x05)

    t.write8(REG_TESTR, 0x0D)

    # Channel-1 TX power from the EEPROM (priv->channels[0].hw_value).
    set_tx_power(t, RfVariant.RTL8225, 1, power)

    # RX antenna default to A.
    write_phy_cck(t, 0x10, 0x9b)          # B variant: 0xDB
    write_phy_ofdm(t, 0x26, 0x90)         # B variant: 0x10

    t.write8(REG_TX_ANTENNA, 0x03)        # B variant: 0x00
    time.sleep(0.001)
    t.write32(0xFF94, 0x3DC00002)         # 0xFF94 = REG_HSSI_PARA

    # Final sensitivity write (row 2 again).
    rfw(0x0c, 0x50)
    write_phy_ofdm(t, 0x0d, rtl8225_gain[2 * 4 + 0])
    write_phy_ofdm(t, 0x1b, rtl8225_gain[2 * 4 + 2])
    write_phy_ofdm(t, 0x1d, rtl8225_gain[2 * 4 + 3])
    write_phy_ofdm(t, 0x23, rtl8225_gain[2 * 4 + 1])
    write_phy_cck(t, 0x41, rtl8225_threshold[2])


# ======================================================================
# RTL8225z2 variant (M2c) — different silicon revision, different init
# sequence and tables.  [SRC] rtl8225.c:571-920
# ======================================================================

# [SRC] rtl8225.c:571-583  (16 rows × 8 = 128 entries)
rtl8225z2_agc = (
    0x5e, 0x5e, 0x5e, 0x5e, 0x5d, 0x5b, 0x59, 0x57, 0x55, 0x53, 0x51, 0x4f,
    0x4d, 0x4b, 0x49, 0x47, 0x45, 0x43, 0x41, 0x3f, 0x3d, 0x3b, 0x39, 0x37,
    0x35, 0x33, 0x31, 0x2f, 0x2d, 0x2b, 0x29, 0x27, 0x25, 0x23, 0x21, 0x1f,
    0x1d, 0x1b, 0x19, 0x17, 0x15, 0x13, 0x11, 0x0f, 0x0d, 0x0b, 0x09, 0x07,
    0x05, 0x03, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x19, 0x19, 0x19, 0x19, 0x19, 0x19, 0x19, 0x19,
    0x19, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x26, 0x27, 0x27, 0x28,
    0x28, 0x29, 0x2a, 0x2a, 0x2a, 0x2b, 0x2b, 0x2b, 0x2c, 0x2c, 0x2c, 0x2d,
    0x2d, 0x2d, 0x2d, 0x2e, 0x2e, 0x2e, 0x2e, 0x2f, 0x2f, 0x2f, 0x30, 0x30,
    0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31,
    0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31, 0x31,
)

# [SRC] rtl8225.c:584-593  (60 entries — not used by rf_init; here for M3+)
rtl8225z2_ofdm = (
    0x10, 0x0d, 0x01, 0x00, 0x14, 0xfb, 0xfb, 0x60,
    0x00, 0x60, 0x00, 0x00, 0x00, 0x5c, 0x00, 0x00,
    0x40, 0x00, 0x40, 0x00, 0x00, 0x00, 0xa8, 0x26,
    0x32, 0x33, 0x07, 0xa5, 0x6f, 0x55, 0xc8, 0xb3,
    0x0a, 0xe1, 0x2C, 0x8a, 0x86, 0x83, 0x34, 0x0f,
    0x4f, 0x24, 0x6f, 0xc2, 0x6b, 0x40, 0x80, 0x00,
    0xc0, 0xc1, 0x58, 0xf1, 0x00, 0xe4, 0x90, 0x3e,
    0x6d, 0x3c, 0xfb, 0x07,
)

# [SRC] rtl8225.c:595-600  (4 rows × 8)
rtl8225z2_tx_power_cck_ch14 = (
    0x36, 0x35, 0x2e, 0x1b, 0x00, 0x00, 0x00, 0x00,
    0x30, 0x2f, 0x29, 0x15, 0x00, 0x00, 0x00, 0x00,
    0x30, 0x2f, 0x29, 0x15, 0x00, 0x00, 0x00, 0x00,
    0x30, 0x2f, 0x29, 0x15, 0x00, 0x00, 0x00, 0x00,
)

# [SRC] rtl8225.c:602-607  (4 rows × 8)
rtl8225z2_tx_power_cck = (
    0x36, 0x35, 0x2e, 0x25, 0x1c, 0x12, 0x09, 0x04,
    0x30, 0x2f, 0x29, 0x21, 0x19, 0x10, 0x08, 0x03,
    0x2b, 0x2a, 0x25, 0x1e, 0x16, 0x0e, 0x07, 0x03,
    0x26, 0x25, 0x21, 0x1b, 0x14, 0x0d, 0x06, 0x03,
)

# [SRC] rtl8225.c:609-616  (36 entries 0x00..0x23 — direct-indexed by power)
rtl8225z2_tx_gain_cck_ofdm = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
    0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b,
    0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11,
    0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
    0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d,
    0x1e, 0x1f, 0x20, 0x21, 0x22, 0x23,
)

# [SRC] rtl8225.c:750-763  (95 entries — last row has 7 + duplicated 0x03bb)
rtl8225z2_rxgain = (
    0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0408, 0x0409,
    0x040a, 0x040b, 0x0502, 0x0503, 0x0504, 0x0505, 0x0540, 0x0541,
    0x0542, 0x0543, 0x0544, 0x0545, 0x0580, 0x0581, 0x0582, 0x0583,
    0x0584, 0x0585, 0x0588, 0x0589, 0x058a, 0x058b, 0x0643, 0x0644,
    0x0645, 0x0680, 0x0681, 0x0682, 0x0683, 0x0684, 0x0685, 0x0688,
    0x0689, 0x068a, 0x068b, 0x068c, 0x0742, 0x0743, 0x0744, 0x0745,
    0x0780, 0x0781, 0x0782, 0x0783, 0x0784, 0x0785, 0x0788, 0x0789,
    0x078a, 0x078b, 0x078c, 0x078d, 0x0790, 0x0791, 0x0792, 0x0793,
    0x0794, 0x0795, 0x0798, 0x0799, 0x079a, 0x079b, 0x079c, 0x079d,
    0x07a0, 0x07a1, 0x07a2, 0x07a3, 0x07a4, 0x07a5, 0x07a8, 0x07a9,
    0x03aa, 0x03ab, 0x03ac, 0x03ad, 0x03b0, 0x03b1, 0x03b2, 0x03b3,
    0x03b4, 0x03b5, 0x03b8, 0x03b9, 0x03ba, 0x03bb, 0x03bb,
)

# [SRC] rtl8225.c:765-773  (7 rows × 3 entries)
rtl8225z2_gain_bg = (
    0x23, 0x15, 0xa5,  # -82-1 dBm
    0x23, 0x15, 0xb5,  # -82-2 dBm
    0x23, 0x15, 0xc5,  # -82-3 dBm
    0x33, 0x15, 0xc5,  # -78 dBm
    0x43, 0x15, 0xc5,  # -74 dBm
    0x53, 0x15, 0xc5,  # -70 dBm
    0x63, 0x15, 0xc5,  # -66 dBm
)


# ----------------------------------------------------------------------
# rtl8225z2_rf_set_tx_power — z2 silicon's TX-power setter.
# [SRC] rtl8225.c:618-672
#
# Kernel reads cck_power / ofdm_power / txpwr_base from the EEPROM.
# We defer EEPROM bit-banging (see [[feedback_defer_efuse_on_bring_up]])
# and accept defaults of zero for M2c — RX-only bring-up works fine.
# ----------------------------------------------------------------------
def rtl8225z2_rf_set_tx_power(
    t: RTL8187Transport,
    channel: int,
    cck_power: int = 0,
    ofdm_power: int = 0,
    txpwr_base: int = 0,
) -> None:
    cck_power = min(cck_power, 15)
    cck_power += txpwr_base & 0xF
    cck_power = min(cck_power, 35)

    if ofdm_power > 15:
        ofdm_power = 25
    else:
        ofdm_power += 10
    ofdm_power += txpwr_base >> 4
    ofdm_power = min(ofdm_power, 35)

    if channel == 14:
        tbl = rtl8225z2_tx_power_cck_ch14
    else:
        tbl = rtl8225z2_tx_power_cck

    # NB: z2 indexes from byte 0 of the chosen table — NO row stride
    # like BCD does. Kernel: `for (i=0; i<8; i++) write(0x44+i, *tmp++)`.
    for i in range(8):
        write_phy_cck(t, 0x44 + i, tbl[i])

    t.write8(REG_TX_GAIN_CCK, rtl8225z2_tx_gain_cck_ofdm[cck_power])
    time.sleep(0.001)

    # ANAPARAM2 ON window.
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG3)
    t.write8(REG_CONFIG3, reg | CONFIG3_ANAPARAM_WRITE)
    t.write32(REG_ANAPARAM2, ANAPARAM2_ON)
    t.write8(REG_CONFIG3, reg & ~CONFIG3_ANAPARAM_WRITE & 0xFF)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)

    write_phy_ofdm(t, 2, 0x42)
    write_phy_ofdm(t, 5, 0x00)
    write_phy_ofdm(t, 6, 0x40)
    write_phy_ofdm(t, 7, 0x00)
    write_phy_ofdm(t, 8, 0x40)

    t.write8(REG_TX_GAIN_OFDM, rtl8225z2_tx_gain_cck_ofdm[ofdm_power])
    time.sleep(0.001)


# ----------------------------------------------------------------------
# rtl8225z2_rf_init — full z2-revision RF bring-up.
# [SRC] rtl8225.c:775-920
# ----------------------------------------------------------------------
def rtl8225z2_rf_init(t: RTL8187Transport, asic_rev: int, power: TxPower) -> None:
    """Port of rtl8225z2_rf_init (rtl8225.c:775-920) — used on z2 silicon.

    Different RF init sequence + different rxgain/gain_bg tables + a
    handful of OFDM init values that diverge from BCD (notably reg 0x0a,
    0x0d, 0x11, 0x1b, 0x1d, 0x21, 0x23, 0x25, plus an extra write at
    0x21=0x37 after sensitivity).

    Reuses the shared ``rtl8225_agc`` table (NOT ``rtl8225z2_agc``,
    despite the name — kernel uses the BCD agc array here).
    """
    def rfw(addr: int, data: int) -> None:
        rtl8225_write(t, addr, data, asic_rev)

    # z2 canonical RF init sequence (different values from BCD).
    rfw(0x0, 0x2BF)
    rfw(0x1, 0xEE0)
    rfw(0x2, 0x44D)
    rfw(0x3, 0x441)
    rfw(0x4, 0x8C3)
    rfw(0x5, 0xC72)
    rfw(0x6, 0x0E6)
    rfw(0x7, 0x82A)
    rfw(0x8, 0x03F)
    rfw(0x9, 0x335)
    rfw(0xa, 0x9D4)
    rfw(0xb, 0x7BB)
    rfw(0xc, 0x850)
    rfw(0xd, 0xCDF)
    rfw(0xe, 0x02B)
    rfw(0xf, 0x114)
    time.sleep(0.100)

    rfw(0x0, 0x1B7)

    for i, val in enumerate(rtl8225z2_rxgain):
        rfw(0x1, i + 1)
        rfw(0x2, val)

    rfw(0x3, 0x080)
    rfw(0x5, 0x004)
    rfw(0x0, 0x0B7)
    rfw(0x2, 0xc4D)

    time.sleep(0.200)
    rfw(0x2, 0x44D)
    time.sleep(0.100)

    if not (rtl8225_read(t, 6) & (1 << 7)):
        rfw(0x02, 0x0C4D)
        time.sleep(0.200)
        rfw(0x02, 0x044D)
        time.sleep(0.100)
        if not (rtl8225_read(t, 6) & (1 << 7)):
            logger.warning(
                "rtl8225z2: RF calibration failed, reg6=0x%x", rtl8225_read(t, 6)
            )

    time.sleep(0.200)

    rfw(0x0, 0x2BF)

    # AGC table (uses the shared rtl8225_agc — NOT rtl8225z2_agc).
    for i, val in enumerate(rtl8225_agc):
        write_phy_ofdm(t, 0xB, val)
        write_phy_ofdm(t, 0xA, 0x80 + i)

    time.sleep(0.001)

    # OFDM PHY register init (40 writes — diverges from BCD on
    # several entries, see comment block in z2_rf_init docstring).
    write_phy_ofdm(t, 0x00, 0x01)
    write_phy_ofdm(t, 0x01, 0x02)
    write_phy_ofdm(t, 0x02, 0x42)
    write_phy_ofdm(t, 0x03, 0x00)
    write_phy_ofdm(t, 0x04, 0x00)
    write_phy_ofdm(t, 0x05, 0x00)
    write_phy_ofdm(t, 0x06, 0x40)
    write_phy_ofdm(t, 0x07, 0x00)
    write_phy_ofdm(t, 0x08, 0x40)
    write_phy_ofdm(t, 0x09, 0xfe)
    write_phy_ofdm(t, 0x0a, 0x08)      # z2: 0x08  (BCD: 0x09)
    write_phy_ofdm(t, 0x0b, 0x80)
    write_phy_ofdm(t, 0x0c, 0x01)
    write_phy_ofdm(t, 0x0d, 0x43)      # z2: 0x43  (BCD: derived from gain table)
    write_phy_ofdm(t, 0x0e, 0xd3)
    write_phy_ofdm(t, 0x0f, 0x38)
    write_phy_ofdm(t, 0x10, 0x84)
    write_phy_ofdm(t, 0x11, 0x07)      # z2: 0x07  (BCD: 0x06)
    write_phy_ofdm(t, 0x12, 0x20)
    write_phy_ofdm(t, 0x13, 0x20)
    write_phy_ofdm(t, 0x14, 0x00)
    write_phy_ofdm(t, 0x15, 0x40)
    write_phy_ofdm(t, 0x16, 0x00)
    write_phy_ofdm(t, 0x17, 0x40)
    write_phy_ofdm(t, 0x18, 0xef)
    write_phy_ofdm(t, 0x19, 0x19)
    write_phy_ofdm(t, 0x1a, 0x20)
    write_phy_ofdm(t, 0x1b, 0x15)      # z2: 0x15  (BCD: 0x76)
    write_phy_ofdm(t, 0x1c, 0x04)
    write_phy_ofdm(t, 0x1d, 0xc5)      # z2: 0xc5  (BCD: derived from gain)
    write_phy_ofdm(t, 0x1e, 0x95)
    write_phy_ofdm(t, 0x1f, 0x75)
    write_phy_ofdm(t, 0x20, 0x1f)
    write_phy_ofdm(t, 0x21, 0x17)      # z2: 0x17  (BCD: 0x27)
    write_phy_ofdm(t, 0x22, 0x16)
    write_phy_ofdm(t, 0x23, 0x80)      # z2: 0x80  (BCD: derived from gain)
    write_phy_ofdm(t, 0x24, 0x46)
    write_phy_ofdm(t, 0x25, 0x00)      # z2: 0x00  (BCD: 0x20)
    write_phy_ofdm(t, 0x26, 0x90)
    write_phy_ofdm(t, 0x27, 0x88)

    # Sensitivity row 3 (-78 dBm) — z2 uses gain_bg[4*3..4*3+2].
    # NB the kernel indexes as [4*3], [4*3+1], [4*3+2] = entries 12,13,14
    # of gain_bg which is (0x43, 0x15, 0xc5) — the -78dBm row.
    write_phy_ofdm(t, 0x0b, rtl8225z2_gain_bg[4 * 3 + 0])
    write_phy_ofdm(t, 0x1b, rtl8225z2_gain_bg[4 * 3 + 1])
    write_phy_ofdm(t, 0x1d, rtl8225z2_gain_bg[4 * 3 + 2])
    write_phy_ofdm(t, 0x21, 0x37)

    # CCK PHY init (28 writes — first 14 same as BCD, last 14 differ).
    write_phy_cck(t, 0x00, 0x98)
    write_phy_cck(t, 0x03, 0x20)
    write_phy_cck(t, 0x04, 0x7e)
    write_phy_cck(t, 0x05, 0x12)
    write_phy_cck(t, 0x06, 0xfc)
    write_phy_cck(t, 0x07, 0x78)
    write_phy_cck(t, 0x08, 0x2e)
    write_phy_cck(t, 0x10, 0x9b)
    write_phy_cck(t, 0x11, 0x88)
    write_phy_cck(t, 0x12, 0x47)
    write_phy_cck(t, 0x13, 0xd0)
    write_phy_cck(t, 0x19, 0x00)
    write_phy_cck(t, 0x1a, 0xa0)
    write_phy_cck(t, 0x1b, 0x08)
    write_phy_cck(t, 0x40, 0x86)
    write_phy_cck(t, 0x41, 0x8d)
    write_phy_cck(t, 0x42, 0x15)
    write_phy_cck(t, 0x43, 0x18)
    write_phy_cck(t, 0x44, 0x36)       # z2: 0x36  (BCD: 0x1f)
    write_phy_cck(t, 0x45, 0x35)       # z2: 0x35  (BCD: 0x1e)
    write_phy_cck(t, 0x46, 0x2e)       # z2: 0x2e  (BCD: 0x1a)
    write_phy_cck(t, 0x47, 0x25)       # z2: 0x25  (BCD: 0x15)
    write_phy_cck(t, 0x48, 0x1c)       # z2: 0x1c  (BCD: 0x10)
    write_phy_cck(t, 0x49, 0x12)       # z2: 0x12  (BCD: 0x0a)
    write_phy_cck(t, 0x4a, 0x09)       # z2: 0x09  (BCD: 0x05)
    write_phy_cck(t, 0x4b, 0x04)       # z2: 0x04  (BCD: 0x02)
    write_phy_cck(t, 0x4c, 0x05)

    # 0xFF5B is TESTR; kernel hits it raw.
    t.write8(0xFF5B, 0x0D)
    time.sleep(0.001)

    set_tx_power(t, RfVariant.RTL8225Z2, 1, power)

    # RX antenna default to A.
    write_phy_cck(t, 0x10, 0x9b)          # B variant: 0xDB
    write_phy_ofdm(t, 0x26, 0x90)         # B variant: 0x10

    t.write8(REG_TX_ANTENNA, 0x03)        # B variant: 0x00
    time.sleep(0.001)
    t.write32(0xFF94, 0x3DC00002)         # = REG_HSSI_PARA


# ----------------------------------------------------------------------
# RF setup — asic_rev + variant, populated by probe.probe() and cached on
# the driver for set_channel dispatch.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RfSetup:
    asic_rev: int
    variant: RfVariant


def build_rf_init(t: RTL8187Transport, setup: RfSetup, power: TxPower):
    """Return a one-arg ``(transport) -> None`` callable wired to the right RF variant +
    the EEPROM TX-power table for the attached hardware. ``setup`` (asic_rev + variant)
    and ``power`` both come from :func:`wifit3.chips.rtl8187.probe.probe`."""
    asic_rev = setup.asic_rev
    if setup.variant is RfVariant.RTL8225Z2:
        def _rf_init(_t: RTL8187Transport) -> None:
            rtl8225z2_rf_init(_t, asic_rev, power)
        return _rf_init

    def _rf_init(_t: RTL8187Transport) -> None:
        rtl8225_rf_init(_t, asic_rev, power)
    return _rf_init
