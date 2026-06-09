"""EFUSE reader + EEPROM-image parser for the RT3070.

The AWUS036NH stores its calibration in an on-die **EFUSE** (no external 93C66),
so the kernel reads it through the EFUSE_CTRL/EFUSE_DATA window rather than the
one-shot USB_EEPROM_READ. The result is a 512-byte image laid out exactly like a
real EEPROM, which ``parse_eeprom`` then decodes.

**The word-offset fix** [[feedback_port_completeness]]: ``rt2800_read_eeprom_efuse``
loops a u16-WORD index (0, 8, 16, …) into ``EFUSE_CTRL_ADDRESS_IN`` — NOT a byte
offset. The shared ``chips/rt2800usb/eeprom.py`` passes a byte offset and so
fetches from double the address on every block past the first; block 0 (the MAC)
happens to be correct, which hid the bug behind a firmware-only gate. We read the
word index, and ``verify_pcap.py rt3070`` catches the wrong one on block 1.
[SRC rt2800lib.c:10955-10963]
"""
from __future__ import annotations

from dataclasses import dataclass

from . import constants as C
from .transport import RT3070Transport


def efuse_detect(t: RT3070Transport) -> bool:
    """EFUSE_CTRL_PRESENT — is calibration stored in EFUSE? [SRC rt2800lib.c:10894-10906]"""
    reg = t.register_read(C.EFUSE_CTRL)
    return bool(C.get_field(reg, C.EFUSE_CTRL_PRESENT))


def _efuse_read_block(t: RT3070Transport, eeprom: bytearray, i: int) -> None:
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


def read_eeprom_efuse(t: RT3070Transport) -> bytes:
    """Probe-time EEPROM read [SRC rt2800usb.c:594-608 rt2800usb_read_eeprom].

    autorun_detect → efuse_detect → 32 word-blocks. The two non-EFUSE branches
    (AutoRun, 93C66 EEPROM) can't fire on this card; ported as explicit errors
    rather than dropped so a different rt2x00 member would surface here, not
    silently mis-read.
    """
    if t.autorun_detect():
        raise NotImplementedError(
            "#TODO untestable: NIC reported AutoRun mode — no AWUS036NH wire path")
    if not efuse_detect(t):
        # 93C66 EEPROM path — not present on this EFUSE card.
        return t.eeprom_read(C.EEPROM_SIZE)

    eeprom = bytearray(C.EEPROM_SIZE)
    for i in range(0, C.EEPROM_SIZE // 2, 8):   # u16-word index: 0, 8, …, 248
        _efuse_read_block(t, eeprom, i)
    return bytes(eeprom)


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

    def word(self, index: int) -> int:
        """u16 at EEPROM word ``index`` [SRC rt2800lib.c rt2800_eeprom_read]."""
        off = index * 2
        return self.raw[off] | (self.raw[off + 1] << 8)


def parse_eeprom(buf: bytes) -> EepromValues:
    """Decode the fields the bring-up needs [SRC rt2800lib.c:11171-11243
    rt2800_init_eeprom]. RF type / chain count come from NIC_CONF0; the chip is
    RF3020 1T1R with these EFUSE values."""
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
    return EepromValues(
        raw=buf,
        mac=mac,
        rf_type=C.get_field(nic_conf0, C.EEPROM_NIC_CONF0_RF_TYPE),
        tx_chain_num=C.get_field(nic_conf0, C.EEPROM_NIC_CONF0_TXPATH),
        rx_chain_num=C.get_field(nic_conf0, C.EEPROM_NIC_CONF0_RXPATH),
        freq_offset=C.get_field(word(C.EEPROM_FREQ), C.EEPROM_FREQ_OFFSET),
        nic_conf1=nic_conf1,
        led_mcu_reg=word(C.EEPROM_FREQ),
        txmixer_gain_24g=txmixer_gain_24g,
        external_lna_bg=bool(C.get_field(nic_conf1, C.EEPROM_NIC_CONF1_EXTERNAL_LNA_2G)),
    )
