"""EFUSE-backed EEPROM reader for rt2800usb.

The rt2800 chips store per-unit calibration data (MAC address, RF cal,
LNA gain, TX power per channel, antenna config, etc.) in an embedded
fuse (EFUSE) array. The kernel reads all 512 bytes at the very start
of bring-up via EFUSE_CTRL's bit-bang protocol — without these values
loaded, downstream BBP/RFCSR writes use chip defaults that often gate
RX.

EFUSE_CTRL protocol (rt2800lib.c:10909-10963). ADDRESS_IN is a u16-WORD
index (the kernel loops i += 8 words), so the fetched fuse address is
byte_offset // 2 while the 16-byte result is stored back at byte_offset:

    for byte_offset in range(0, 512, 16):
        # 1) Set up read request
        reg = read32(EFUSE_CTRL)
        reg.ADDRESS_IN = byte_offset // 2  # bits 25:17 — u16-word index
        reg.MODE = 0                    # bits 7:6
        reg.KICK = 1                    # bit 30
        write32(EFUSE_CTRL, reg)
        # 2) Poll KICK until clear (chip signals "read complete")
        while read32(EFUSE_CTRL) & KICK: pass
        # 3) Read 16 bytes (4 dwords), HIGH dwords first into low offsets
        eeprom[offset+ 0..3] = read32(EFUSE_DATA3)    # LE bytes
        eeprom[offset+ 4..7] = read32(EFUSE_DATA2)
        eeprom[offset+ 8..11] = read32(EFUSE_DATA1)
        eeprom[offset+12..15] = read32(EFUSE_DATA0)

EEPROM word layout (rt2800lib.c:308-347 rt2800_eeprom_map):

    word 0x02  MAC_ADDR_0  (bytes 0,1)
    word 0x03  MAC_ADDR_1  (bytes 2,3)
    word 0x04  MAC_ADDR_2  (bytes 4,5)
    word 0x1A  NIC_CONF0   (TX/RX path counts in low byte)
    word 0x1B  NIC_CONF1   (BT coex, antenna diversity, ext LNA bits)
    word 0x1D  FREQ        (freq_offset low byte → RFCSR17.CODE)
    word 0x22  LNA         (LNA_BG, LNA_A0, LNA_A1, LNA_A2)
    word 0x23  RSSI_BG     (per-path RSSI offsets for 2.4 GHz)

Each 16-bit "word" is 2 bytes, LE.
"""
from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from .constants import REGISTER_BUSY_COUNT
from .transport import RT2800USBTransport

logger = logging.getLogger(__name__)

EFUSE_CTRL = 0x0580
EFUSE_DATA0 = 0x0590
EFUSE_DATA1 = 0x0594
EFUSE_DATA2 = 0x0598
EFUSE_DATA3 = 0x059C

EFUSE_CTRL_ADDRESS_IN = 0x03FE0000
EFUSE_CTRL_MODE = 0x000000C0
EFUSE_CTRL_KICK = 0x40000000
EFUSE_CTRL_PRESENT = 0x80000000

EEPROM_SIZE = 0x0200          # 512 bytes = 256 words
EFUSE_READ_CHUNK = 16         # bytes per EFUSE read iteration

# Word offsets in the EEPROM buffer (kernel rt2800_eeprom_map)
EEPROM_OFFSET_MAC_ADDR_0 = 0x02
EEPROM_OFFSET_NIC_CONF0 = 0x1A
EEPROM_OFFSET_NIC_CONF1 = 0x1B
EEPROM_OFFSET_FREQ = 0x1D
EEPROM_OFFSET_LED_AG_CONF = 0x1E     # per-band LED config words (enable_radio LED tail)
EEPROM_OFFSET_LED_ACT_CONF = 0x1F
EEPROM_OFFSET_LED_POLARITY = 0x20
EEPROM_OFFSET_LNA = 0x22
EEPROM_OFFSET_RSSI_BG = 0x23
EEPROM_OFFSET_RSSI_BG2 = 0x24    # OFFSET2 (low) + LNA_A1 (high)
EEPROM_OFFSET_RSSI_A = 0x25      # A OFFSET0 (low) + OFFSET1 (high)
EEPROM_OFFSET_RSSI_A2 = 0x26     # A2 OFFSET2 (low) + LNA_A2 (high)

# Per-channel + per-rate TX-power word offsets (kernel rt2800_eeprom_map,
# non-ext format — RT3572/RT5572 are not RT3593/RT3883). Each array is
# indexed by BYTE (kernel `s8 *default_power1; default_power1[i]`), so the
# byte offset into the EFUSE dump is (word << 1). [SRC] rt2800lib.c:308-347
EEPROM_OFFSET_EIRP_MAX_TX_POWER = 0x27
EEPROM_OFFSET_TXPOWER_DELTA = 0x28
EEPROM_OFFSET_TXPOWER_BG1 = 0x29     # byte 0x52: 2.4 GHz chain-0 power, ch1..14
EEPROM_OFFSET_TXPOWER_BG2 = 0x30     # byte 0x60: 2.4 GHz chain-1 power, ch1..14
EEPROM_OFFSET_TXPOWER_A1 = 0x3C      # byte 0x78: 5 GHz chain-0 power, indexed i-14
EEPROM_OFFSET_TXPOWER_A2 = 0x53      # byte 0xa6: 5 GHz chain-1 power, indexed i-14
EEPROM_OFFSET_TXPOWER_BYRATE = 0x6F  # byte 0xde: per-rate power, 9 words
EEPROM_TXPOWER_BYRATE_SIZE = 9       # [SRC] rt2800.h:2941

# txpower_to_dev clamp bounds [SRC] rt2800.h:3171-3174 + POWER_BOUND clamp in
# the RF55xx/RF53xx config (rt2800lib.c:3299) applied at write time in chan.py.
MIN_G_TXPOWER = 0
MAX_G_TXPOWER = 31
MIN_A_TXPOWER = -7
MAX_A_TXPOWER = 15

# EIRP power-limit gate [SRC] rt2800lib.c:11318-11322 rt2800_init_eeprom:
# CAPABILITY_POWER_LIMIT is set when EEPROM_EIRP_MAX_TX_POWER_2GHZ < 0x50.
EEPROM_EIRP_MAX_TX_POWER_2GHZ = 0x00FF   # low byte of the EIRP_MAX word
EEPROM_EIRP_MAX_TX_POWER_5GHZ = 0xFF00   # high byte
EIRP_MAX_TX_POWER_LIMIT = 0x50
EEPROM_TXPOWER_BYRATE_RATE = tuple(0x000F << (4 * n) for n in range(4))  # nibble RATE0..3

# RT5592 IQ calibration byte offsets — kernel uses rt2x00_eeprom_byte()
# directly, so these are BYTE offsets into the EFUSE dump, not word
# offsets. [SRC] rt2800.h:2963-2988
EEPROM_BYTE_IQ_GAIN_CAL_TX0_2G              = 0x130
EEPROM_BYTE_IQ_PHASE_CAL_TX0_2G             = 0x131
EEPROM_BYTE_IQ_GAIN_CAL_TX1_2G              = 0x133
EEPROM_BYTE_IQ_PHASE_CAL_TX1_2G             = 0x134
EEPROM_BYTE_RF_IQ_COMPENSATION_CONTROL      = 0x13C
EEPROM_BYTE_RF_IQ_IMBALANCE_COMPENSATION    = 0x13D
EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH36_TO_CH64_5G  = 0x144
EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH36_TO_CH64_5G = 0x145
EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH100_TO_CH138_5G  = 0x146
EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH100_TO_CH138_5G = 0x147
EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH140_TO_CH165_5G  = 0x148
EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH140_TO_CH165_5G = 0x149
EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH36_TO_CH64_5G  = 0x14A
EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH36_TO_CH64_5G = 0x14B
EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH100_TO_CH138_5G  = 0x14C
EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH100_TO_CH138_5G = 0x14D
EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH140_TO_CH165_5G  = 0x14E
EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH140_TO_CH165_5G = 0x14F


def _set_field32(reg: int, mask: int, value: int) -> int:
    shift = (mask & -mask).bit_length() - 1
    return ((reg & ~mask) | ((value << shift) & mask)) & 0xFFFFFFFF


def efuse_detect(t: RT2800USBTransport) -> bool:
    """Check EFUSE_CTRL.PRESENT bit — kernel rt2800_efuse_detect."""
    return bool(t.read32(EFUSE_CTRL) & EFUSE_CTRL_PRESENT)


def _efuse_read_chunk(t: RT2800USBTransport, byte_offset: int) -> bytes:
    """Read 16 bytes from EFUSE starting at byte_offset.

    Mirrors rt2800_efuse_read (rt2800lib.c:10909-10953) — request,
    poll, read 4 data regs in HIGH→LOW order, each LE.
    """
    reg = t.read32(EFUSE_CTRL)
    # ADDRESS_IN is a u16-word index, not a byte address: rt2800_efuse_read
    # loops in word units (i += 8) and EFUSE_CTRL_ADDRESS_IN = FIELD32(0x03fe0000).
    # The 16-byte result is stored at byte_offset in the buffer, but the fuse
    # itself must be addressed by word. [SRC] rt2800lib.c:10955
    reg = _set_field32(reg, EFUSE_CTRL_ADDRESS_IN, byte_offset // 2)
    reg = _set_field32(reg, EFUSE_CTRL_MODE, 0)
    reg = _set_field32(reg, EFUSE_CTRL_KICK, 1)
    t.write32(EFUSE_CTRL, reg)

    # Poll until KICK clears
    for _ in range(REGISTER_BUSY_COUNT):
        cur = t.read32(EFUSE_CTRL)
        if not (cur & EFUSE_CTRL_KICK):
            break
        time.sleep(0.000_05)
    else:
        raise IOError(f"EFUSE read at offset 0x{byte_offset:04x}: KICK never cleared")

    # Read 4 dwords. Kernel comment: "Apparently the data is read from
    # end to start" — DATA3 first, then DATA2, DATA1, DATA0.
    # Each dword is 4 bytes LE.
    chunk = bytearray(16)
    for i, addr in enumerate((EFUSE_DATA3, EFUSE_DATA2, EFUSE_DATA1, EFUSE_DATA0)):
        v = t.read32(addr)
        chunk[i * 4: i * 4 + 4] = struct.pack("<I", v)
    return bytes(chunk)


def read_eeprom_efuse(t: RT2800USBTransport) -> bytes:
    """Dump all 512 bytes of EFUSE-backed EEPROM.

    Wire order mirrors rt2800usb_read_eeprom (rt2800usb.c:594-608): autorun_detect
    (skips FW/EEPROM read in AutoRun mode) → efuse_detect → the 32-block loop. The
    autorun probe is the first vendor op after the MAC_CSR0 chip-id read."""
    if t.autorun_detect():
        raise NotImplementedError(
            "NIC reported AutoRun mode — no wire path validated on this card")
    if not efuse_detect(t):
        raise IOError("EFUSE_CTRL.PRESENT bit not set — no EFUSE on this chip")
    buf = bytearray(EEPROM_SIZE)
    for offset in range(0, EEPROM_SIZE, EFUSE_READ_CHUNK):
        buf[offset: offset + EFUSE_READ_CHUNK] = _efuse_read_chunk(t, offset)
    return bytes(buf)


# ----------------------------------------------------------------------
# RT5592 IQ calibration — kernel rt2800_iq_calibrate (rt2800lib.c:4026-4110)
# reads 6 bytes per tune from the EEPROM dump: 2 each (gain, phase) for
# TX0 and TX1, plus 2 global IQ compensation/imbalance bytes. The 2 G/
# 5 G byte addresses differ per band, hence the helper that maps a
# channel to the right offsets.
#
# Kernel convention: 0xFF means "unprogrammed" → use 0 instead. We
# materialise that here so the channel-tune code path stays simple.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class IqCalibration:
    """Per-band IQ cal bytes pulled from the EFUSE dump at parse time."""
    # 2.4 GHz pair (TX0 + TX1, gain + phase each).
    tx0_gain_2g: int
    tx0_phase_2g: int
    tx1_gain_2g: int
    tx1_phase_2g: int
    # 5 GHz UNII-1/2 (ch 36-64).
    tx0_gain_5g_lo: int
    tx0_phase_5g_lo: int
    tx1_gain_5g_lo: int
    tx1_phase_5g_lo: int
    # 5 GHz UNII-2-ext (ch 100-138).
    tx0_gain_5g_mid: int
    tx0_phase_5g_mid: int
    tx1_gain_5g_mid: int
    tx1_phase_5g_mid: int
    # 5 GHz UNII-3 (ch 140-165).
    tx0_gain_5g_hi: int
    tx0_phase_5g_hi: int
    tx1_gain_5g_hi: int
    tx1_phase_5g_hi: int
    # Global RF IQ compensation + imbalance.
    rf_iq_comp: int
    rf_iq_imbal: int

    def for_channel(self, channel: int) -> "IqCalChannel":
        """Pick the band-specific cal bytes for `channel`."""
        if channel <= 14:
            return IqCalChannel(
                tx0_gain=self.tx0_gain_2g,
                tx0_phase=self.tx0_phase_2g,
                tx1_gain=self.tx1_gain_2g,
                tx1_phase=self.tx1_phase_2g,
                rf_iq_comp=self.rf_iq_comp,
                rf_iq_imbal=self.rf_iq_imbal,
            )
        if 36 <= channel <= 64:
            return IqCalChannel(
                tx0_gain=self.tx0_gain_5g_lo,
                tx0_phase=self.tx0_phase_5g_lo,
                tx1_gain=self.tx1_gain_5g_lo,
                tx1_phase=self.tx1_phase_5g_lo,
                rf_iq_comp=self.rf_iq_comp,
                rf_iq_imbal=self.rf_iq_imbal,
            )
        if 100 <= channel <= 138:
            return IqCalChannel(
                tx0_gain=self.tx0_gain_5g_mid,
                tx0_phase=self.tx0_phase_5g_mid,
                tx1_gain=self.tx1_gain_5g_mid,
                tx1_phase=self.tx1_phase_5g_mid,
                rf_iq_comp=self.rf_iq_comp,
                rf_iq_imbal=self.rf_iq_imbal,
            )
        if 140 <= channel <= 165:
            return IqCalChannel(
                tx0_gain=self.tx0_gain_5g_hi,
                tx0_phase=self.tx0_phase_5g_hi,
                tx1_gain=self.tx1_gain_5g_hi,
                tx1_phase=self.tx1_phase_5g_hi,
                rf_iq_comp=self.rf_iq_comp,
                rf_iq_imbal=self.rf_iq_imbal,
            )
        # Channels outside the kernel's known sub-bands — kernel falls
        # through to `cal = 0` in this case.
        return IqCalChannel(
            tx0_gain=0, tx0_phase=0, tx1_gain=0, tx1_phase=0,
            rf_iq_comp=self.rf_iq_comp,
            rf_iq_imbal=self.rf_iq_imbal,
        )


@dataclass(frozen=True)
class IqCalChannel:
    """Resolved-for-this-channel IQ cal bytes (kernel rt2x00_eeprom_byte
    on the per-band offset). All bytes have already been mapped through
    the kernel's `0xFF → 0` unprogrammed-fallback convention."""
    tx0_gain: int
    tx0_phase: int
    tx1_gain: int
    tx1_phase: int
    rf_iq_comp: int
    rf_iq_imbal: int


def _eeprom_byte_raw(buf: bytes, offset: int) -> int:
    """rt2x00_eeprom_byte — the raw EEPROM byte, no fallback. The kernel writes
    the TX0/TX1 IQ gain/phase cal bytes verbatim (BBP159), even 0xFF."""
    return buf[offset] if offset < len(buf) else 0xFF


def _eeprom_byte_or_zero(buf: bytes, offset: int) -> int:
    """rt2x00_eeprom_byte with the kernel's 0xFF → 0 fallback. ONLY the two
    global IQ comp/imbalance control bytes get this (rt2800lib.c:4103, 4109
    `cal != 0xff ? cal : 0`); the per-band TX0/TX1 gain/phase bytes do NOT —
    use _eeprom_byte_raw for those."""
    b = buf[offset] if offset < len(buf) else 0xFF
    return b if b != 0xFF else 0


# ----------------------------------------------------------------------
# Parsers for the EEPROM byte buffer
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EepromValues:
    mac_address: bytes        # 6 bytes
    nic_conf0: int            # u16
    nic_conf1: int            # u16
    freq_offset: int          # u8 (low byte of FREQ word)
    lna_gain_bg: int          # u8 LNA_BG — 2.4 GHz LNA gain
    lna_gain_a: int           # u8 LNA_A0 — 5 GHz ch 15–64 LNA gain
    rssi_bg_offset0: int
    rssi_bg_offset1: int
    rssi_bg_offset2: int = 0
    rssi_a_offset0: int = 0
    rssi_a_offset1: int = 0
    rssi_a_offset2: int = 0
    lna_gain_a1: int = 0      # 5 GHz ch 65–128 LNA (RSSI_BG2 high byte)
    lna_gain_a2: int = 0      # 5 GHz ch > 128 LNA (RSSI_A2 high byte)
    iq_cal: "IqCalibration | None" = None    # RT5592-only; None for other chips
    raw: bytes = b""          # full 512-byte EFUSE dump, for TX-power byte/word access

    # NIC_CONF1 capability bits — kernel `rt2x00_has_cap_*`, set from the
    # EEPROM_NIC_CONF1 fields in rt2800_init_eeprom (rt2800lib.c:11280-11296).
    # [SRC] rt2800.h:2706-2720 EEPROM_NIC_CONF1_* field definitions.
    @property
    def has_cap_bt_coexist(self) -> bool:
        return bool(self.nic_conf1 & 0x4000)         # BT_COEXIST, bit 14

    @property
    def has_cap_external_lna_bg(self) -> bool:
        return bool(self.nic_conf1 & 0x0004)         # EXTERNAL_LNA_2G, bit 2

    @property
    def has_cap_external_lna_a(self) -> bool:
        return bool(self.nic_conf1 & 0x0008)         # EXTERNAL_LNA_5G, bit 3

    @property
    def rxpath(self) -> int:
        """RX chain count, from NIC_CONF0 RXPATH field (bits[3:0]).

        Special case: when EFUSE looks unburned, return kernel's
        documented default of 2 RX paths. Kernel rt2800_validate_eeprom
        (rt2800lib.c:11050) only applies this default when the read is
        0xFFFF; we extend it to additional patterns we've observed on
        unburned dongles in the wild:

          * NIC_CONF0 = 0x0000 — RT3572 (AWUS051NH v2) reads zeros.
          * NIC_CONF0 = 0x0F0F — RT5572 (Panda PAU09 N600) reads this
            "0xF per nibble" factory pattern; gives txpath=0 (impossible)
            and rxpath=15 (impossible, max is 3T3R).

        Robust check: if either field is outside [1, 3], the entire word
        is unburned. The kernel runs an unburned RT3572 as a single RX
        chain — [WIRE] aireplay.pcap on this card writes RFCSR1=0xf1
        (RX1 + RX2 powered down) — so the validated default is 1 RX.
        [SRC] rt2800.h:2681
        """
        if self._nic_conf0_looks_unburned():
            return 1
        return self.nic_conf0 & 0x000F

    @property
    def txpath(self) -> int:
        """TX chain count, from NIC_CONF0 TXPATH field (bits[7:4]).
        Same unburned-EFUSE handling as ``rxpath``: kernel's default
        is 1 TX path. [SRC] rt2800.h:2682, rt2800lib.c:11052"""
        if self._nic_conf0_looks_unburned():
            return 1
        return (self.nic_conf0 & 0x00F0) >> 4

    def _nic_conf0_looks_unburned(self) -> bool:
        """True if NIC_CONF0 looks unprogrammed: all-zero, all-set, or
        decoded txpath/rxpath outside the physical [1, 3] range."""
        if self.nic_conf0 in (0x0000, 0xFFFF, 0x0F0F):
            return True
        raw_rxpath = self.nic_conf0 & 0x000F
        raw_txpath = (self.nic_conf0 & 0x00F0) >> 4
        return raw_rxpath < 1 or raw_rxpath > 3 or raw_txpath < 1 or raw_txpath > 3

    @property
    def looks_unburned(self) -> bool:
        """Public gate for the TX-power fallback: an unburned EFUSE has no
        per-channel calibration, so the driver keeps its wire-derived
        hardcoded TX power (see driver._channel_kwargs) instead of decoding
        garbage bytes. Same signal as the txpath/rxpath unburned handling —
        the user's AWUS051NH v2 (RT3572) reads NIC_CONF0=0x0000."""
        return self._nic_conf0_looks_unburned()

    def word(self, word_index: int) -> int:
        """16-bit LE word at ``word_index`` of the raw EFUSE dump."""
        off = word_index * 2
        return self.raw[off] | (self.raw[off + 1] << 8)

    def power_byte(self, word_index: int, i: int) -> int:
        """Signed per-channel TX-power byte ``i`` of the s8 array based at
        ``word_index`` [SRC rt2800lib.c:11923-11957 ``default_power1[i]``].
        Stored as s8 in the EEPROM."""
        b = self.raw[word_index * 2 + i]
        return b - 0x100 if b >= 0x80 else b

    @property
    def power_limit(self) -> bool:
        """CAPABILITY_POWER_LIMIT — set when the burned EIRP max is below the
        regulatory limit, which switches on the per-rate EIRP compensation.
        [SRC] rt2800lib.c:11318-11322 rt2800_init_eeprom."""
        eirp = self.word(EEPROM_OFFSET_EIRP_MAX_TX_POWER) & EEPROM_EIRP_MAX_TX_POWER_2GHZ
        return eirp < EIRP_MAX_TX_POWER_LIMIT

    @property
    def rf_type(self) -> int:
        """RF companion-chip id from NIC_CONF0.RF_TYPE (bits[11:8]) — kernel
        EEPROM_NIC_CONF0_RF_TYPE = FIELD16(0x0f00). This is how RT28xx/RT30xx
        (incl. RT3572) encode which RF chip is fitted, distinct from the RT MAC
        silicon read from MAC_CSR0. 0 on an unburned EEPROM (retail RT3572
        dongles read NIC_CONF0=0x0000). [SRC] rt2800.h:2683, rt2800lib.c:11201."""
        return (self.nic_conf0 & 0x0F00) >> 8

    @property
    def chip_id_word(self) -> int:
        """EEPROM word 0 = EEPROM_CHIP_ID — the RF id source for RT5390/RT5392/
        RT3290/RT6352 silicon (those read EEPROM_CHIP_ID, not NIC_CONF0.RF_TYPE).
        [SRC] rt2800lib.c:11187-11191, rt2800_eeprom_map[EEPROM_CHIP_ID]=0x0000."""
        return self.word(0)


def _word(eeprom: bytes, word_offset: int) -> int:
    """Read a 16-bit LE word at the given word offset (× 2 = byte offset)."""
    byte_offset = word_offset * 2
    return eeprom[byte_offset] | (eeprom[byte_offset + 1] << 8)


def _sanitize_rssi_offset(raw: int) -> int:
    """kernel rt2800_validate_eeprom: an RSSI offset with abs > 10 is junk → 0."""
    return raw if abs(raw) <= 10 else 0


def _sanitize_lna(raw: int, default: int) -> int:
    """kernel rt2800_validate_eeprom: LNA_A1/A2 of 0x00 or 0xff → LNA_A0."""
    return default if raw in (0x00, 0xFF) else raw


def txpower_to_dev(channel: int, txpower: int) -> int:
    """Clamp a per-channel EEPROM TX-power byte to the chip's device range
    [SRC rt2800lib.c:4112-4129 rt2800_txpower_to_dev]. RT3572/RT5572 are not
    RT3593/RT3883, so the EEPROM_TXPOWER_ALC field + the _3593 bounds don't
    apply. An unburned byte (0xFF→-1, 0x00→0) clamps into range, NOT to a
    hardcoded value — the hardcoded fallback is a driver-level unburned choice
    (see driver._channel_kwargs), applied before this."""
    if channel <= 14:
        return max(MIN_G_TXPOWER, min(txpower, MAX_G_TXPOWER))
    return max(MIN_A_TXPOWER, min(txpower, MAX_A_TXPOWER))


# Default crystal-compensation value to use when EFUSE freq_offset is
# unburned (0x00 or 0xFF). The kernel only checks 0xFFFF in
# rt2800_validate_eeprom (rt2800lib.c:11088) and falls through with
# freq_offset=0; we extend the check to 0x00 because freq_offset=0
# with an unburned chip puts the synth several MHz off-center for the
# requested channel and yields 0 URBs of decodable RX on our test hw.
#
# Picking the value:
#   * Kernel pcap evidence — `usb_dumps/captures_rt2800usb_rt3572/`:
#       - rt2800lib.c:7929 init_rfcsr_3572 writes a hardcoded 0x3C to
#         RFCSR23 as the chip's post-init magic value (NOT a freq trim).
#       - rt2800lib.c:2722 (config_channel_rf3052) does the real RMW:
#         read RFCSR23, set FREQ_OFFSET field from EFUSE, write back.
#         Pcap captures-1/2/3 all show 0x35 → that dongle's burned
#         EFUSE FREQ low byte = 0x35 = 53 decimal.
#   * Sweep on USER's unburned AWUS051NH v2 (M-A1, 2026-05-20):
#         freq_offset 0/20/40/60 → 0 / 27% / 41% / 48% parse rate.
#         Monotonically increasing across the tested range → best of
#         the sweep is 60 (838 parsed / 18 BSSIDs / 10 s).
#
# 60 is the empirical peak on the user's hw, 53 is one other dongle's
# burn value. Per-unit crystal varies, so neither is universal.
# UNBURNED_FREQ_OFFSET_DEFAULT below picks 60 (sweep peak on user's
# tested chip); future runtime auto-scan would be per-chip principled
# but adds ~1 s startup latency. Tracked in the
# "deferred-from-M-A1" list in project_rt2800usb_rt3572 memory.
UNBURNED_FREQ_OFFSET_DEFAULT = 60


def parse_eeprom(eeprom: bytes) -> EepromValues:
    """Extract the subset of EEPROM values that monitor-mode RX needs."""
    mac0 = _word(eeprom, EEPROM_OFFSET_MAC_ADDR_0)
    mac1 = _word(eeprom, EEPROM_OFFSET_MAC_ADDR_0 + 1)
    mac2 = _word(eeprom, EEPROM_OFFSET_MAC_ADDR_0 + 2)
    mac = bytes((
        mac0 & 0xFF, (mac0 >> 8) & 0xFF,
        mac1 & 0xFF, (mac1 >> 8) & 0xFF,
        mac2 & 0xFF, (mac2 >> 8) & 0xFF,
    ))
    nic0 = _word(eeprom, EEPROM_OFFSET_NIC_CONF0)
    nic1 = _word(eeprom, EEPROM_OFFSET_NIC_CONF1)
    freq = _word(eeprom, EEPROM_OFFSET_FREQ) & 0xFF
    # Unburned-EFUSE handling: 0x00 and 0xFF both indicate "no crystal
    # calibration was burned at the factory". Apply a sensible default
    # so the chip is approximately on-frequency. See module-level
    # UNBURNED_FREQ_OFFSET_DEFAULT comment for the experimental basis.
    if freq in (0x00, 0xFF):
        logger.warning(
            "EFUSE freq_offset=0x%02x looks unburned — applying default %d "
            "(sweep peak on M-A1 test hw; kernel-pcap evidence on a "
            "burned RT3572 used 53). Override via --freq-offset if poor.",
            freq, UNBURNED_FREQ_OFFSET_DEFAULT,
        )
        freq = UNBURNED_FREQ_OFFSET_DEFAULT
    lna_word = _word(eeprom, EEPROM_OFFSET_LNA)
    lna_bg = lna_word & 0xFF
    lna_a0 = (lna_word >> 8) & 0xFF          # LNA_A0; also the default for A1/A2
    rssi_bg = _word(eeprom, EEPROM_OFFSET_RSSI_BG)
    rssi_bg2 = _word(eeprom, EEPROM_OFFSET_RSSI_BG2)
    rssi_a = _word(eeprom, EEPROM_OFFSET_RSSI_A)
    rssi_a2 = _word(eeprom, EEPROM_OFFSET_RSSI_A2)

    # RT5592 IQ cal bytes. Always parsed (only RT5572 uses them; other
    # chips just get an unused IqCalibration struct full of zeros).
    iq_cal = IqCalibration(
        tx0_gain_2g=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX0_2G),
        tx0_phase_2g=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX0_2G),
        tx1_gain_2g=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX1_2G),
        tx1_phase_2g=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX1_2G),
        tx0_gain_5g_lo=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH36_TO_CH64_5G),
        tx0_phase_5g_lo=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH36_TO_CH64_5G),
        tx1_gain_5g_lo=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH36_TO_CH64_5G),
        tx1_phase_5g_lo=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH36_TO_CH64_5G),
        tx0_gain_5g_mid=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH100_TO_CH138_5G),
        tx0_phase_5g_mid=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH100_TO_CH138_5G),
        tx1_gain_5g_mid=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH100_TO_CH138_5G),
        tx1_phase_5g_mid=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH100_TO_CH138_5G),
        tx0_gain_5g_hi=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX0_CH140_TO_CH165_5G),
        tx0_phase_5g_hi=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX0_CH140_TO_CH165_5G),
        tx1_gain_5g_hi=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_GAIN_CAL_TX1_CH140_TO_CH165_5G),
        tx1_phase_5g_hi=_eeprom_byte_raw(eeprom, EEPROM_BYTE_IQ_PHASE_CAL_TX1_CH140_TO_CH165_5G),
        rf_iq_comp=_eeprom_byte_or_zero(eeprom, EEPROM_BYTE_RF_IQ_COMPENSATION_CONTROL),
        rf_iq_imbal=_eeprom_byte_or_zero(eeprom, EEPROM_BYTE_RF_IQ_IMBALANCE_COMPENSATION),
    )

    return EepromValues(
        mac_address=mac,
        nic_conf0=nic0,
        nic_conf1=nic1,
        freq_offset=freq,
        lna_gain_bg=lna_bg,
        lna_gain_a=lna_a0,
        lna_gain_a1=_sanitize_lna((rssi_bg2 >> 8) & 0xFF, lna_a0),
        lna_gain_a2=_sanitize_lna((rssi_a2 >> 8) & 0xFF, lna_a0),
        rssi_bg_offset0=_sanitize_rssi_offset(rssi_bg & 0xFF),
        rssi_bg_offset1=_sanitize_rssi_offset((rssi_bg >> 8) & 0xFF),
        rssi_bg_offset2=_sanitize_rssi_offset(rssi_bg2 & 0xFF),
        rssi_a_offset0=_sanitize_rssi_offset(rssi_a & 0xFF),
        rssi_a_offset1=_sanitize_rssi_offset((rssi_a >> 8) & 0xFF),
        rssi_a_offset2=_sanitize_rssi_offset(rssi_a2 & 0xFF),
        iq_cal=iq_cal,
        raw=bytes(eeprom),
    )


# ----------------------------------------------------------------------
# RF companion-chip identification — the Ralink discriminator.
#
# The rt2800 family separates two ids: the RT *MAC silicon* (read from
# MAC_CSR0, drives rt2800_init_bbp / rt2800_init_rfcsr) and the *RF chip*
# (encoded in the EEPROM, drives rt2800_config_channel). For most RT28xx/
# RT30xx the RF chip is NIC_CONF0.RF_TYPE; RT5390/RT5392/RT3290/RT6352 read
# EEPROM_CHIP_ID; RT5350/RT5592/RT3352/RT3883 hardcode their sole RF. The
# same silicon can therefore pair with different RF chips (e.g. RT3070 →
# RF3020/RF3021/RF3022/RF3070), so the RF chip — not the silicon — is the
# real config_channel discriminator.
#
# RF chip ids [SRC] rt2800.h:49-73.
# ----------------------------------------------------------------------
RF3020 = 0x0005
RF3021 = 0x0007
RF3022 = 0x0008
RF3052 = 0x0009
RF3053 = 0x000D
RF5592 = 0x000F
RF3320 = 0x000B
RF3322 = 0x000C
RF3070 = 0x3070
RF3290 = 0x3290
RF3853 = 0x3853
RF5350 = 0x5350
RF5360 = 0x5360
RF5362 = 0x5362
RF5370 = 0x5370
RF5372 = 0x5372
RF5390 = 0x5390
RF5392 = 0x5392

RF_NAMES = {
    RF3020: "RF3020", RF3021: "RF3021", RF3022: "RF3022", RF3052: "RF3052",
    RF3053: "RF3053", RF5592: "RF5592", RF3320: "RF3320", RF3322: "RF3322",
    RF3070: "RF3070", RF3290: "RF3290", RF3853: "RF3853", RF5350: "RF5350",
    RF5360: "RF5360", RF5362: "RF5362", RF5370: "RF5370", RF5372: "RF5372",
    RF5390: "RF5390", RF5392: "RF5392",
}

# RF chips this port has a config_channel path for. RT3572 only ever reaches
# RF3052; the RF5390/5392/5592 entries cover the family-shared tune code
# retained from before the per-chip driver split. [SRC] rt2800lib.c:4185-4220
# (rt2800_config_channel RF switch: RF3052→rf3052, RF539x→rf53xx, RF5592→rf55xx).
_PORTED_RF_CHIPS = frozenset({RF3052, RF5390, RF5392, RF5592})


@dataclass(frozen=True)
class RfChip:
    """RF companion chip resolved from the runtime EEPROM.

    ``rf_id`` is the raw kernel RF constant (0 when the EEPROM read is
    unburned/unreadable). ``ported`` says whether this driver has a
    config_channel path for it. An unrecognised/unburned read is NOT ported
    but is still run on the silicon's default tune (the kernel -ENODEVs on an
    unknown RF; we do not, so an erased-EEPROM retail dongle still comes up)."""
    rf_id: int
    name: str
    ported: bool


def resolve_rf_chip(silicon_id: int, ev: EepromValues) -> RfChip:
    """1:1 port of the RF-chipset identification block of rt2800_init_eeprom
    (rt2800lib.c:11182-11235): RT5390/RT5392/RT3290/RT6352 take the RF id from
    EEPROM_CHIP_ID; RT3352/RT3883/RT5350/RT5592 hardcode their sole RF; all
    others (incl. RT3572) read NIC_CONF0.RF_TYPE. Unlike the kernel this does
    NOT fail on an unknown RF — an unburned EEPROM (RF_TYPE==0) is expected on
    retail RT3572 dongles, and the caller runs it on the silicon default."""
    from .constants import (
        RT_RT3290, RT_RT3352, RT_RT3883, RT_RT5350, RT_RT5390, RT_RT5392,
        RT_RT5592, RT_RT6352,
    )
    if silicon_id in (RT_RT5390, RT_RT5392, RT_RT3290, RT_RT6352):
        rf = ev.chip_id_word
    elif silicon_id == RT_RT3352:
        rf = RF3322
    elif silicon_id == RT_RT3883:
        rf = RF3853
    elif silicon_id == RT_RT5350:
        rf = RF5350
    elif silicon_id == RT_RT5592:
        rf = RF5592
    else:
        rf = ev.rf_type
    return RfChip(rf_id=rf, name=RF_NAMES.get(rf, f"0x{rf:04x}"), ported=rf in _PORTED_RF_CHIPS)
