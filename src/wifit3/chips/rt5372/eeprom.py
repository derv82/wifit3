"""EFUSE reader + EEPROM-image parser for the RT5372 (RT5392).

The PAU05/PAU06 store calibration in an on-die **EFUSE** (no external 93C66),
so the kernel reads it through the EFUSE_CTRL/EFUSE_DATA window rather than the
one-shot USB_EEPROM_READ. The result is a 512-byte image laid out exactly like a
real EEPROM, which ``parse_eeprom`` then decodes.

**The word-offset fix** [[feedback_port_completeness]]: ``rt2800_read_eeprom_efuse``
loops a u16-WORD index (0, 8, 16, …) into ``EFUSE_CTRL_ADDRESS_IN`` — NOT a byte
offset. The shared ``chips/rt2800usb/eeprom.py`` passes a byte offset and so
fetches from double the address on every block past the first; block 0 (the MAC)
happens to be correct, which hid the bug behind a firmware-only gate — and on the
RT5392 it lands freq_offset/NIC_CONF0/LNA/RSSI on the wrong bytes. This clean-room
reader uses the word index (the reason this driver exists standalone), and
``verify_pcap.py rt5372`` catches the wrong one on block 1. [SRC rt2800lib.c:10955-10963]
"""
from __future__ import annotations

from dataclasses import dataclass

from . import constants as C
from .transport import RT5372Transport


def efuse_detect(t: RT5372Transport) -> bool:
    """EFUSE_CTRL_PRESENT — is calibration stored in EFUSE? [SRC rt2800lib.c:10894-10906]"""
    reg = t.register_read(C.EFUSE_CTRL)
    return bool(C.get_field(reg, C.EFUSE_CTRL_PRESENT))


def _efuse_read_block(t: RT5372Transport, eeprom: bytearray, i: int) -> None:
    """Read one 16-byte EFUSE block into ``eeprom`` at u16-word index ``i``.

    [SRC rt2800lib.c:10909-10953 rt2800_efuse_read]. Data comes back "end to
    start" (DATA3 first), each u32 stored little-endian; ``i`` is the word index
    so the byte offset is ``i * 2``.
    """
    reg = t.register_read(C.EFUSE_CTRL)
    reg = C.set_field(reg, C.EFUSE_CTRL_ADDRESS_IN, i)
    reg = C.set_field(reg, C.EFUSE_CTRL_MODE, 0)
    reg = C.set_field(reg, C.EFUSE_CTRL_KICK, 1)
    t.register_write(C.EFUSE_CTRL, reg)

    t.regbusy_read(C.EFUSE_CTRL, C.EFUSE_CTRL_KICK)

    byte_off = i * 2
    for n, data_reg in enumerate((C.EFUSE_DATA3, C.EFUSE_DATA2,
                                  C.EFUSE_DATA1, C.EFUSE_DATA0)):
        reg = t.register_read(data_reg)
        pos = byte_off + n * 4
        eeprom[pos:pos + 4] = (reg & 0xFFFFFFFF).to_bytes(4, "little")


def read_eeprom_efuse(t: RT5372Transport) -> bytes:
    """Probe-time EEPROM read [SRC rt2800usb.c:594-608 rt2800usb_read_eeprom].

    autorun_detect → efuse_detect → 32 word-blocks. The two non-EFUSE branches
    (AutoRun, 93C66 EEPROM) can't fire on this card; ported as explicit errors
    rather than dropped so a different rt2x00 member would surface here, not
    silently mis-read.
    """
    if t.autorun_detect():
        raise NotImplementedError(
            "#TODO untestable: NIC reported AutoRun mode — no PAU05/PAU06 wire path")
    if not efuse_detect(t):
        # 93C66 EEPROM path — not present on this EFUSE card.
        return t.eeprom_read(C.EEPROM_SIZE)

    eeprom = bytearray(C.EEPROM_SIZE)
    for i in range(0, C.EEPROM_SIZE // 2, 8):   # u16-word index: 0, 8, …, 248
        _efuse_read_block(t, eeprom, i)
    return bytes(eeprom)


def _write_word(buf: bytearray, index: int, value: int) -> None:
    buf[index * 2] = value & 0xFF
    buf[index * 2 + 1] = (value >> 8) & 0xFF


def validate_eeprom(buf: bytes) -> bytearray:
    """Fill defaults for blank/invalid EEPROM fields [SRC rt2800lib.c:11026-11169
    rt2800_validate_eeprom], the kernel's pre-``init_eeprom`` fix-up. Operates on the
    in-RAM image only (no chip write); ``parse_eeprom`` then decodes the result.

    #TODO untestable on this card: its EFUSE is factory-burned, so every guard below is
    false (no 0xffff fields, offsets in range) and the buffer is returned unchanged. The
    branch is exercised only by a blank/counterfeit EEPROM (e.g. the RT3572 we've seen).
    """
    out = bytearray(buf)

    def word(i: int) -> int:
        return out[i * 2] | (out[i * 2 + 1] << 8)

    nic0 = word(C.EEPROM_NIC_CONF0)
    if nic0 == 0xFFFF:
        nic0 = C.set_field(0, C.EEPROM_NIC_CONF0_RXPATH, 2)
        nic0 = C.set_field(nic0, C.EEPROM_NIC_CONF0_TXPATH, 1)
        nic0 = C.set_field(nic0, C.EEPROM_NIC_CONF0_RF_TYPE, C.RF2820)
        _write_word(out, C.EEPROM_NIC_CONF0, nic0)
    # The RT2860/RT2872 RXPATH>2 clamp is a different (non-RF30xx) silicon — N/A here.

    if word(C.EEPROM_NIC_CONF1) == 0xFFFF:
        _write_word(out, C.EEPROM_NIC_CONF1, 0)   # zeroing every field == word 0

    freq = word(C.EEPROM_FREQ)
    if (freq & 0x00FF) == 0x00FF:
        freq = C.set_field(freq, C.EEPROM_FREQ_OFFSET, 0)
        _write_word(out, C.EEPROM_FREQ, freq)
    if (freq & 0xFF00) == 0xFF00:
        freq = C.set_field(freq, C.EEPROM_FREQ_LED_MODE, C.LED_MODE_TXRX_ACTIVITY)
        freq = C.set_field(freq, C.EEPROM_FREQ_LED_POLARITY, 0)
        _write_word(out, C.EEPROM_FREQ, freq)
        _write_word(out, C.EEPROM_LED_AG_CONF, 0x5555)
        _write_word(out, C.EEPROM_LED_ACT_CONF, 0x2221)
        _write_word(out, C.EEPROM_LED_POLARITY, 0xA9F8)

    # lna0 is the fallback gain for the other (zero/0xff) per-chain LNA fields.
    default_lna_gain = C.get_field(word(C.EEPROM_LNA), C.EEPROM_LNA_A0)

    def _clamp_offsets(word_idx: int, fields: tuple[int, ...],
                       lna_field: int = 0, lna_default: int = 0) -> None:
        w = word(word_idx)
        for f in fields:
            if C.get_field(w, f) > 10:            # kernel abs() on the u8 field (quirk-faithful)
                w = C.set_field(w, f, 0)
        if lna_field and C.get_field(w, lna_field) in (0x00, 0xFF):
            w = C.set_field(w, lna_field, lna_default)
        _write_word(out, word_idx, w)

    _clamp_offsets(C.EEPROM_RSSI_BG, (C.EEPROM_RSSI_BG_OFFSET0, C.EEPROM_RSSI_BG_OFFSET1))
    _clamp_offsets(C.EEPROM_RSSI_BG2, (C.EEPROM_RSSI_BG2_OFFSET2,),
                   C.EEPROM_RSSI_BG2_LNA_A1, default_lna_gain)
    # 5 GHz RSSI_A/A2: validated too (cached-buffer only; this card never tunes 5 GHz).
    _clamp_offsets(C.EEPROM_RSSI_A, (C.EEPROM_RSSI_A_OFFSET0, C.EEPROM_RSSI_A_OFFSET1))
    _clamp_offsets(C.EEPROM_RSSI_A2, (C.EEPROM_RSSI_A2_OFFSET2,),
                   C.EEPROM_RSSI_A2_LNA_A2, default_lna_gain)
    # EXT_LNA2 fix-up is RT3593/RT3883-only (out of scope).
    return out


@dataclass
class EepromValues:
    """Decoded EEPROM image. ``raw`` keeps the full 512 bytes so later bring-up
    steps can read any word via :meth:`word`, exactly like ``rt2800_eeprom_read``."""

    raw: bytes
    mac: bytes
    rf_type: int
    tx_chain_num: int
    rx_chain_num: int
    freq_offset: int
    nic_conf1: int
    led_mcu_reg: int        # EEPROM_FREQ word; LED mode/polarity for MCU_LED [rt2800lib.c:11312]
    txmixer_gain_24g: int   # [rt2800lib.c:10996-11008 rt2800_get_txmixer_gain_24g]
    external_lna_bg: bool   # NIC_CONF1 EXTERNAL_LNA_2G [rt2800lib.c:11282]
    lna_gain_bg: int        # EEPROM_LNA BG byte [rt2800lib.c:2408 config_lna_gain]
    external_tx_alc: bool    # NIC_CONF1 EXTERNAL_TX_ALC [rt2800lib.c:4578 gain cal gate]
    power_limit: bool        # EIRP_MAX_2GHZ < limit ⇒ CAPABILITY_POWER_LIMIT [rt2800lib.c:11320]
    ant_diversity: int       # NIC_CONF1 ANT_DIVERSITY [rt2800lib.c:2365 config_ant]
    bt_coexist: bool         # NIC_CONF1 BT_COEXIST → CAPABILITY_BT_COEXIST [rt2800lib.c:11295]
    rssi_offset_bg: tuple[int, int, int]   # RX RSSI per-path offsets [rt2800lib.c:867-878]

    def word(self, index: int) -> int:
        """u16 at EEPROM word ``index`` [SRC rt2800lib.c rt2800_eeprom_read]."""
        off = index * 2
        return self.raw[off] | (self.raw[off + 1] << 8)

    def power_byte(self, word_index: int, i: int) -> int:
        """Signed per-channel TX-power byte ``i`` of the array based at ``word_index``
        [SRC rt2800lib.c:11923-11936 ``default_power1[i] = eeprom_addr(...)[i]``].
        The EEPROM stores these as ``s8``."""
        b = self.raw[word_index * 2 + i]
        return b - 0x100 if b >= 0x80 else b


def parse_eeprom(buf: bytes) -> EepromValues:
    """Decode the fields the bring-up needs [SRC rt2800lib.c:11171-11243
    rt2800_init_eeprom]. For RT53xx the RF id is read from EEPROM_CHIP_ID (word 0),
    NOT the 4-bit NIC_CONF0 RF_TYPE field [SRC rt2800lib.c:11184-11201]; chain counts
    still come from NIC_CONF0. PAU05/PAU06 decode RF5372, txpath=rxpath=2 (2T2R),
    freq_offset=59 — the correct word-offset read. (The rt2800usb imitation's byte-offset
    bug read NIC_CONF0=0x1a19 → rxpath=9 → its "unburned" heuristic forced 1T1R.)"""
    def word(index: int) -> int:
        off = index * 2
        return buf[off] | (buf[off + 1] << 8)

    nic_conf0 = word(C.EEPROM_NIC_CONF0)
    nic_conf1 = word(C.EEPROM_NIC_CONF1)
    mac = bytes(buf[C.EEPROM_MAC_ADDR_0 * 2:C.EEPROM_MAC_ADDR_0 * 2 + 6])

    # rt2800_get_txmixer_gain_24g: VAL field if low byte != 0xff, else 0 [rt2800lib.c:10996].
    txmixer_bg = word(C.EEPROM_TXMIXER_GAIN_BG)
    txmixer_gain_24g = (C.get_field(txmixer_bg, C.EEPROM_TXMIXER_GAIN_BG_VAL)
                        if (txmixer_bg & 0x00FF) != 0x00FF else 0)
    # CAPABILITY_POWER_LIMIT: EIRP 2.4 GHz max below the limit ⇒ honor it [rt2800lib.c:11320].
    eirp_2g = C.get_field(word(C.EEPROM_EIRP_MAX_TX_POWER), C.EEPROM_EIRP_MAX_TX_POWER_2GHZ)
    return EepromValues(
        raw=buf,
        mac=mac,
        rf_type=word(C.EEPROM_CHIP_ID),   # RT53xx: RF id from CHIP_ID [rt2800lib.c:11187-11191]
        tx_chain_num=C.get_field(nic_conf0, C.EEPROM_NIC_CONF0_TXPATH),
        rx_chain_num=C.get_field(nic_conf0, C.EEPROM_NIC_CONF0_RXPATH),
        freq_offset=C.get_field(word(C.EEPROM_FREQ), C.EEPROM_FREQ_OFFSET),
        nic_conf1=nic_conf1,
        led_mcu_reg=word(C.EEPROM_FREQ),
        txmixer_gain_24g=txmixer_gain_24g,
        external_lna_bg=bool(C.get_field(nic_conf1, C.EEPROM_NIC_CONF1_EXTERNAL_LNA_2G)),
        lna_gain_bg=C.get_field(word(C.EEPROM_LNA), C.EEPROM_LNA_BG),
        external_tx_alc=bool(C.get_field(nic_conf1, C.EEPROM_NIC_CONF1_EXTERNAL_TX_ALC)),
        power_limit=eirp_2g < C.EIRP_MAX_TX_POWER_LIMIT,
        ant_diversity=C.get_field(nic_conf1, C.EEPROM_NIC_CONF1_ANT_DIVERSITY),
        bt_coexist=bool(C.get_field(nic_conf1, C.EEPROM_NIC_CONF1_BT_COEXIST)),
        rssi_offset_bg=(
            C.get_field(word(C.EEPROM_RSSI_BG), C.EEPROM_RSSI_BG_OFFSET0),
            C.get_field(word(C.EEPROM_RSSI_BG), C.EEPROM_RSSI_BG_OFFSET1),
            C.get_field(word(C.EEPROM_RSSI_BG2), C.EEPROM_RSSI_BG2_OFFSET2),
        ),
    )
