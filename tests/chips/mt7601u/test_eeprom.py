"""EEPROM decode tests for the MT7601U port.

The transport is mocked, so these run without hardware: only the decode logic is
under test, not the USB encoding (scripts/porting/ covers that against the pcap).
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.eeprom import (
    _is_valid_ether_addr,
    _random_ether_addr,
    EepromError,
    MT7601UEeprom,
    PowerPerRate,
    int_to_s6,
    s6_to_int,
    s6_validate,
)


EFUSE_IMAGE_SIZE = max(C.MT7601U_EEPROM_SIZE, C.MT_EE_USAGE_MAP_END + 1)
"""The 256-byte EEPROM and the usage map at 0x1e0..0x1fc are separate regions of
one efuse address space (eeprom.c:69 reads the map with MT_EE_PHYSICAL_READ), so the
fake has to cover both."""


class FakeTransport:
    """Serves programmed efuse bytes through the real efuse_read register dance."""

    def __init__(self, efuse_bytes: bytes = b"", alc_limit: int = 0x1F):
        self.efuse_bytes = bytearray(efuse_bytes)
        self.alc_limit = alc_limit
        self.writes: list[tuple[int, int]] = []
        self._reads: dict[int, int] = {
            C.MT_TX_ALC_CFG_0: C._field_prep(C.MT_TX_ALC_CFG_0_LIMIT_0, alc_limit)}
        self._efuse_addr = 0

    def rr(self, offset: int) -> int:
        if offset == C.MT_EFUSE_CTRL:
            return 0                      # KICK clear, AOUT not all-ones
        if C.MT_EFUSE_DATA_BASE <= offset <= C.MT_EFUSE_DATA_BASE + 0xC:
            word = (offset - C.MT_EFUSE_DATA_BASE) // 4
            chunk = bytes(self.efuse_bytes[self._efuse_addr:self._efuse_addr + 16])
            return int.from_bytes(chunk[word * 4:word * 4 + 4].ljust(4, b"\x00"), "little")
        return self._reads.get(offset, 0)

    def wr(self, offset: int, val: int) -> None:
        # The port-facing wr() hands over the whole 32-bit value; splitting it into
        # two USB transfers is the transport's job (usb.c:157), not the fake's.
        self.writes.append((offset, val))
        if offset == C.MT_EFUSE_CTRL and val & C.MT_EFUSE_CTRL_KICK:
            self._efuse_addr = C._field_get(C.MT_EFUSE_CTRL_AIN, val)

    def fce_wr(self, offset: int, val: int) -> None:
        self.writes.append((offset, val))


def make_eeprom(overrides: dict[int, int] | None = None) -> bytes:
    """A 256-byte EEPROM image with the fields the decoder reads."""
    ee = bytearray(b"\xff" * EFUSE_IMAGE_SIZE)
    # The usage map sits outside the 256-byte block; mark it fully used so
    # physical_size_check accepts the image.
    ee[C.MT_EE_USAGE_MAP_START:C.MT_EE_USAGE_MAP_START + C.MT_EFUSE_USAGE_MAP_SIZE] = \
        b"\x01" * C.MT_EFUSE_USAGE_MAP_SIZE
    ee[C.MT_EE_VERSION_EE] = 0x0C
    ee[C.MT_EE_VERSION_FAE] = 0x00
    ee[C.MT_EE_MAC_ADDR:C.MT_EE_MAC_ADDR + 6] = bytes.fromhex("200db0305159")
    # NIC_CONF_0/1 at 0x34/0x36: RX/TX path 1, no TX ALC, no HW RF ctrl.
    ee[C.MT_EE_NIC_CONF_0:C.MT_EE_NIC_CONF_0 + 2] = (1 | (1 << 4)).to_bytes(2, "little")
    ee[C.MT_EE_NIC_CONF_1:C.MT_EE_NIC_CONF_1 + 2] = (0x40).to_bytes(2, "little")
    ee[C.MT_EE_COUNTRY_REGION] = 0x05
    ee[C.MT_EE_FREQ_OFFSET] = 0x2D
    ee[C.MT_EE_FREQ_OFFSET_COMPENSATION] = 0x04
    ee[C.MT_EE_RSSI_OFFSET] = 0x03
    ee[C.MT_EE_REF_TEMP] = 0xF9
    ee[C.MT_EE_LNA_GAIN] = 0x00
    ee[C.MT_EE_TX_POWER_DELTA_BW40] = 0x09
    for i in range(14):
        ee[C.MT_EE_TX_POWER_OFFSET + i] = 0x0B
    # Per-rate words, little-endian, all inside the 6-bit s6 encoding (<= 0x3f) so
    # s6_validate does not mask the test values away.
    for i, word in enumerate([0x04030201, 0x08070605, 0x0c0b0a09, 0, 0]):
        ee[C.MT_EE_TX_POWER_BYRATE(i):C.MT_EE_TX_POWER_BYRATE(i) + 4] = \
            word.to_bytes(4, "little")
    for key, val in (overrides or {}).items():
        ee[key] = val
    return bytes(ee)


def run_read(tp: FakeTransport) -> MT7601UEeprom:
    """Drive the real read() path end to end, efuse served by the fake transport."""
    ee = MT7601UEeprom(tp)
    ee.read()
    return ee


class TestS6:
    @pytest.mark.parametrize("raw,expected", [
        (0x00, 0), (0x1F, 0x1F), (0x3F, -1), (0x20, -32), (0x3E, -2),
    ])
    def test_s6_to_int_sign_extends(self, raw: int, expected: int) -> None:
        assert s6_to_int(raw) == expected

    def test_s6_validate_masks_to_six_bits(self) -> None:
        assert s6_validate(0xFF) == 0x3F

    @pytest.mark.parametrize("val,expected", [
        (0, 0), (5, 5), (-5, 0x3B), (-0x21, 0x20), (0x20, 0x1F),
    ])
    def test_int_to_s6_clamps(self, val: int, expected: int) -> None:
        assert int_to_s6(val) == expected


class TestDecode:
    def test_reads_mac_and_version(self) -> None:
        tp = FakeTransport(make_eeprom())
        ee = run_read(tp)
        assert ee.macaddr == bytes.fromhex("200db0305159")
        assert ee.version_ee == 0x0C
        assert ee.version_fae == 0x00

    def test_programs_mac_registers(self) -> None:
        tp = FakeTransport(make_eeprom())
        run_read(tp)
        # DW1 carries the last two MAC bytes plus U2ME=0xff (mac.c:23).
        assert (C.MT_MAC_ADDR_DW0, 0x30B00D20) in tp.writes
        assert (C.MT_MAC_ADDR_DW1, 0x00FF5951) in tp.writes

    def test_country_region_selects_channel_plan(self) -> None:
        for region, start, num in [(0x00, 1, 11), (0x01, 1, 13), (0x05, 1, 14), (0x07, 5, 9)]:
            tp = FakeTransport(make_eeprom({C.MT_EE_COUNTRY_REGION: region}))
            params = run_read(tp).ee
            assert (params.reg.start, params.reg.num) == (start, num), region

    def test_unmapped_country_region_falls_back_to_ch1_14(self) -> None:
        tp = FakeTransport(make_eeprom({C.MT_EE_COUNTRY_REGION: 0x1F}))
        params = run_read(tp).ee
        assert (params.reg.start, params.reg.num) == (1, 14)

    def test_country_region_32_and_33_map_to_late_table(self) -> None:
        for region, start, num in [(0x20, 1, 11), (0x21, 1, 14)]:
            tp = FakeTransport(make_eeprom({C.MT_EE_COUNTRY_REGION: region}))
            params = run_read(tp).ee
            assert (params.reg.start, params.reg.num) == (start, num), region

    def test_freq_offset_is_signed_by_compensation(self) -> None:
        # comp bit 7 set -> subtract the low 7 bits
        tp = FakeTransport(make_eeprom({
            C.MT_EE_FREQ_OFFSET: 0x10, C.MT_EE_FREQ_OFFSET_COMPENSATION: 0x84}))
        assert run_read(tp).ee.rf_freq_off == 0x10 - 4
        # comp bit 7 clear -> add
        tp = FakeTransport(make_eeprom({
            C.MT_EE_FREQ_OFFSET: 0x10, C.MT_EE_FREQ_OFFSET_COMPENSATION: 0x04}))
        assert run_read(tp).ee.rf_freq_off == 0x10 + 4

    def test_ref_temp_and_lna_gain_sign_extend(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        assert params.ref_temp == -7            # 0xF9
        assert params.lna_gain == 0

    def test_rssi_offset_out_of_range_zeroed(self) -> None:
        tp = FakeTransport(make_eeprom({C.MT_EE_RSSI_OFFSET: 0x40, C.MT_EE_RSSI_OFFSET + 1: 0x03}))
        assert run_read(tp).ee.rssi_offset == [0, 3]

    def test_rssi_offset_in_range_kept(self) -> None:
        tp = FakeTransport(make_eeprom({C.MT_EE_RSSI_OFFSET: 0xFA, C.MT_EE_RSSI_OFFSET + 1: 0x03}))
        assert run_read(tp).ee.rssi_offset == [-6, 3]

    def test_channel_power_from_eeprom_table(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        assert params.chan_pwr == [11] * 14

    def test_negative_channel_power_uses_default(self) -> None:
        # 0xF6 sign-extends to -10, below the 0 floor, so the default replaces it.
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_TX_POWER_OFFSET] = 0xF6
        params = run_read(FakeTransport(bytes(ee))).ee
        assert params.chan_pwr[0] == C.MT7601U_DEFAULT_TX_POWER

    def test_channel_power_over_alc_limit_uses_default(self) -> None:
        # 0x7F -> 127, above the fake's 0x1f ALC limit.
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_TX_POWER_OFFSET] = 0x7F
        tp = FakeTransport(bytes(ee))
        params = run_read(tp).ee
        assert params.chan_pwr[0] == C.MT7601U_DEFAULT_TX_POWER

    def test_unwritten_channel_power_field_reads_as_zero(self) -> None:
        # 0xff is field_valid() == False -> field_validate() == 0, which is in range.
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_TX_POWER_OFFSET] = 0xFF
        params = run_read(FakeTransport(bytes(ee))).ee
        assert params.chan_pwr[0] == 0

    def test_tssi_disabled_when_alc_not_enabled(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        assert params.tssi_enabled is False
        assert params.tssi_data.slope == 0

    def test_tssi_enabled_when_alc_enabled(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_NIC_CONF_1 + 1] |= C.MT_EE_NIC_CONF_1_TX_ALC_EN >> 8   # bit 13
        ee[C.MT_EE_TX_TSSI_SLOPE] = 0x2A
        params = run_read(FakeTransport(bytes(ee))).ee
        assert params.tssi_enabled is True
        assert params.tssi_data.slope == 0x2A

    def test_tssi_disabled_when_temp_tx_alc_set(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_NIC_CONF_1 + 1] |= C.MT_EE_NIC_CONF_1_TX_ALC_EN >> 8
        ee[C.MT_EE_NIC_CONF_1] |= C.MT_EE_NIC_CONF_1_TEMP_TX_ALC
        assert run_read(FakeTransport(bytes(ee))).ee.tssi_enabled is False

    def test_tssi_disabled_when_nic_conf1_all_ones(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_NIC_CONF_1:C.MT_EE_NIC_CONF_1 + 2] = b"\xff\xff"
        assert run_read(FakeTransport(bytes(ee))).ee.tssi_enabled is False

    def test_tssi_uses_single_target_power_for_all_channels(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_NIC_CONF_1 + 1] |= C.MT_EE_NIC_CONF_1_TX_ALC_EN >> 8
        ee[C.MT_EE_TX_TSSI_TARGET_POWER] = 0x18
        params = run_read(FakeTransport(bytes(ee))).ee
        assert params.chan_pwr == [0x18] * 14

    def test_per_rate_power_unpacks_two_rates_per_word(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        # Each 32-bit word packs one rate per byte, low byte first (eeprom.c:269-287).
        t = params.power_rate_table
        assert [r.bw20 for r in t.cck] == [0x01, 0x02]
        assert [r.bw20 for r in t.ofdm] == [0x03, 0x04, 0x05, 0x06]
        assert [r.bw20 for r in t.ht] == [0x07, 0x08, 0x09, 0x0a]

    def test_per_rate_bw40_is_bw20_plus_delta(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        cc = params.power_rate_table.cck[0]
        assert cc.bw40 == cc.bw20         # 0x09 has bit 7 clear -> delta 0 (eeprom.c:296)

    def test_per_rate_bw40_delta_is_signed(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_TX_POWER_DELTA_BW40] = 0xC9      # bit 7 set, bit 6 set -> -9 clamped to -8
        params = run_read(FakeTransport(bytes(ee))).ee
        cc = params.power_rate_table.cck[0]
        assert cc.bw40 == cc.bw20 - 8

    def test_real_cck_bw20_saved_for_channel_14_fixup(self) -> None:
        params = run_read(FakeTransport(make_eeprom())).ee
        assert params.real_cck_bw20 == [params.power_rate_table.cck[0].bw20,
                                        params.power_rate_table.cck[1].bw20]

    def test_per_rate_power_written_to_tx_pwr_cfg(self) -> None:
        tp = FakeTransport(make_eeprom())
        run_read(tp)
        # All five EEPROM words are written straight through (eeprom.c:323); the
        # `if (~val)` guard only skips an all-ones word.
        assert (C.MT_TX_PWR_CFG_0, 0x04030201) in tp.writes
        assert (C.MT_TX_PWR_CFG_1, 0x08070605) in tp.writes
        assert (C.MT_TX_PWR_CFG_2, 0x0C0B0A09) in tp.writes
        assert (C.MT_TX_PWR_CFG_3, 0) in tp.writes
        assert (C.MT_TX_PWR_CFG_4, 0) in tp.writes
        # _extra_power_over_mac folds the deltas back in (eeprom.c:243-246).
        assert (C.MT_TX_PWR_CFG_7, 0) in tp.writes
        assert (C.MT_TX_PWR_CFG_9, 0) in tp.writes

    def test_all_ones_per_rate_power_skips_write(self) -> None:
        ee = bytearray(make_eeprom())
        ee[C.MT_EE_TX_POWER_BYRATE(0):C.MT_EE_TX_POWER_BYRATE(0) + 4] = b"\xff" * 4
        tp = FakeTransport(bytes(ee))
        run_read(tp)
        assert not any(off == C.MT_TX_PWR_CFG_0 for off, _ in tp.writes)


class TestPhysicalSizeCheck:
    """A zero byte in the usage map means free space (eeprom.c:81); the card needs at
    least 5 USED bytes, so a map that is nearly all-zero is the rejected case."""

    @staticmethod
    def _transport_with_map(usage_map: bytes) -> FakeTransport:
        tp = FakeTransport()
        tp.efuse_bytes = bytearray(EFUSE_IMAGE_SIZE)
        tp.efuse_bytes[C.MT_EE_USAGE_MAP_START:
                       C.MT_EE_USAGE_MAP_START + C.MT_EFUSE_USAGE_MAP_SIZE] = usage_map
        return tp

    def test_map_with_five_used_bytes_is_accepted(self) -> None:
        # 5 free bytes (zeros) then 24 used (0x01): cnt_free = 5, 29-5 = 24, not < 5.
        MT7601UEeprom(self._transport_with_map(b"\x00" * 5 + b"\x01" * 24)).physical_size_check()

    def test_map_mostly_free_raises(self) -> None:
        tp = self._transport_with_map(b"\x01" + b"\x00" * 28)
        with pytest.raises(EepromError, match="default EEPROM file"):
            MT7601UEeprom(tp).physical_size_check()

    def test_all_free_raises(self) -> None:
        tp = self._transport_with_map(b"\x00" * C.MT_EFUSE_USAGE_MAP_SIZE)
        with pytest.raises(EepromError, match="default EEPROM file"):
            MT7601UEeprom(tp).physical_size_check()

    def test_fully_used_map_is_accepted(self) -> None:
        tp = self._transport_with_map(b"\x01" * C.MT_EFUSE_USAGE_MAP_SIZE)
        MT7601UEeprom(tp).physical_size_check()


class TestPowerPerRate:
    def test_default_instance_is_zeroed(self) -> None:
        rate = PowerPerRate()
        assert (rate.raw, rate.bw20, rate.bw40) == (0, 0, 0)

class TestMacAddressValidation:
    """mac.c:15-20. is_valid_ether_addr rejects a group address as well as all-zero,
    and mac.c:16 installs a random one rather than carrying on."""

    @pytest.mark.parametrize("addr,valid", [
        (bytes.fromhex("200db0305159"), True),
        (bytes.fromhex("000000000000"), False),
        (bytes.fromhex("ffffffffffff"), False),
        (bytes.fromhex("010203040506"), False),      # group bit set in byte 0
        (bytes.fromhex("020304050607"), True),       # locally administered, not group
    ])
    def test_group_and_zero_addresses_are_refused(self, addr: bytes, valid: bool) -> None:
        assert _is_valid_ether_addr(addr) is valid

    def test_a_random_address_is_unicast_and_locally_administered(self) -> None:
        for _ in range(32):
            addr = _random_ether_addr()
            assert len(addr) == 6
            assert not addr[0] & 0x01                 # never a group address
            assert addr[0] & 0x02                     # locally administered
            assert _is_valid_ether_addr(addr)
