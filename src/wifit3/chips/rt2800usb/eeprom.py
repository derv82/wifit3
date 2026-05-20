"""EFUSE-backed EEPROM reader for rt2800usb.

The rt2800 chips store per-unit calibration data (MAC address, RF cal,
LNA gain, TX power per channel, antenna config, etc.) in an embedded
fuse (EFUSE) array. The kernel reads all 512 bytes at the very start
of bring-up via EFUSE_CTRL's bit-bang protocol — without these values
loaded, downstream BBP/RFCSR writes use chip defaults that often gate
RX.

EFUSE_CTRL protocol (rt2800lib.c:10909-10963):

    for byte_offset in range(0, 512, 16):
        # 1) Set up read request
        reg = read32(EFUSE_CTRL)
        reg.ADDRESS_IN = byte_offset    # bits 25:17
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
EEPROM_OFFSET_LNA = 0x22
EEPROM_OFFSET_RSSI_BG = 0x23


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
    reg = _set_field32(reg, EFUSE_CTRL_ADDRESS_IN, byte_offset)
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
    """Dump all 512 bytes of EFUSE-backed EEPROM."""
    if not efuse_detect(t):
        raise IOError("EFUSE_CTRL.PRESENT bit not set — no EFUSE on this chip")
    buf = bytearray(EEPROM_SIZE)
    for offset in range(0, EEPROM_SIZE, EFUSE_READ_CHUNK):
        buf[offset: offset + EFUSE_READ_CHUNK] = _efuse_read_chunk(t, offset)
    return bytes(buf)


# ----------------------------------------------------------------------
# Parsers for the EEPROM byte buffer
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EepromValues:
    mac_address: bytes        # 6 bytes
    nic_conf0: int            # u16
    nic_conf1: int            # u16
    freq_offset: int          # u8 (low byte of FREQ word)
    lna_gain_bg: int          # u8 (low byte of LNA word — 2.4 GHz LNA gain)
    lna_gain_a: int           # u8 (high byte of LNA word — 5 GHz LNA-A gain)
    rssi_bg_offset0: int
    rssi_bg_offset1: int

    # NIC_CONF1 capability bits — kernel `rt2x00_has_cap_*`.
    # [SRC] rt2800.h:1815-1828 (EEPROM_NIC_CONF1 layout)
    @property
    def has_cap_bt_coexist(self) -> bool:
        return bool(self.nic_conf1 & 0x2000)         # bit 13

    @property
    def has_cap_external_lna_bg(self) -> bool:
        return bool(self.nic_conf1 & 0x0100)         # bit 8

    @property
    def has_cap_external_lna_a(self) -> bool:
        return bool(self.nic_conf1 & 0x0200)         # bit 9

    @property
    def rxpath(self) -> int:
        """RX chain count, from NIC_CONF0 RXPATH field (bits[3:0]).

        Special case: unburned EFUSE (NIC_CONF0 = 0x0000 or 0xFFFF)
        returns kernel's documented default of 2 RX paths. Kernel
        rt2800_validate_eeprom (rt2800lib.c:11050) only applies this
        default when the read is 0xFFFF; we extend it to 0x0000 because
        on Windows we've observed RT3572 chips that EFUSE-readback
        zeros for the NIC_CONF fields (genuinely unburned vs. erased).
        Without this override, set_channel_3572 powers down the
        secondary chains via RFCSR1.RX1/2_PD, the chip's PHY ends up
        misconfigured for the actual 2T2R hardware, and all RX frames
        decode with FCS errors. [SRC] rt2800.h:2681
        """
        rxpath = self.nic_conf0 & 0x000F
        if self.nic_conf0 in (0x0000, 0xFFFF):
            return 2     # kernel default for unburned NIC_CONF0
        return rxpath

    @property
    def txpath(self) -> int:
        """TX chain count, from NIC_CONF0 TXPATH field (bits[7:4]).
        Same unburned-EFUSE handling as ``rxpath``: kernel's default
        is 1 TX path. [SRC] rt2800.h:2682, rt2800lib.c:11052"""
        if self.nic_conf0 in (0x0000, 0xFFFF):
            return 1     # kernel default for unburned NIC_CONF0
        return (self.nic_conf0 & 0x00F0) >> 4


def _word(eeprom: bytes, word_offset: int) -> int:
    """Read a 16-bit LE word at the given word offset (× 2 = byte offset)."""
    byte_offset = word_offset * 2
    return eeprom[byte_offset] | (eeprom[byte_offset + 1] << 8)


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
    lna_a = (lna_word >> 8) & 0xFF
    rssi_bg = _word(eeprom, EEPROM_OFFSET_RSSI_BG)
    return EepromValues(
        mac_address=mac,
        nic_conf0=nic0,
        nic_conf1=nic1,
        freq_offset=freq,
        lna_gain_bg=lna_bg,
        lna_gain_a=lna_a,
        rssi_bg_offset0=rssi_bg & 0xFF,
        rssi_bg_offset1=(rssi_bg >> 8) & 0xFF,
    )
