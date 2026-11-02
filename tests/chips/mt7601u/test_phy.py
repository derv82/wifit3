"""RF/BBP window and channel-tune tests for the MT7601U port.

No hardware: the transport is faked, so these pin the byte layouts and the
control-flow order. The wire bytes are checked against the monitor-mode capture by
scripts/chips/mt7601u/verify_tune.py (55 recorded tunes).
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.eeprom import MT7601UEepromParams
from wifit3.chips.mt7601u.mcu import MT7601UMcu
from wifit3.chips.mt7601u.phy import (
    BBP_CH14_REG,
    BBP_LNA_REGS,
    FREQ_PLAN,
    FREQ_PLAN_BASE_REG,
    MT7601UPhy,
)


class FakeTransport:
    """Records every op; KICK/BUSY always reads clear so polls succeed."""

    def __init__(self, rf_csr: int = 0, bbp_csr: int = 0, alc: int = 0x2F2F0007):
        self.ops: list[tuple] = []
        self.payloads: list[bytes] = []
        # The RF and BBP windows address a register FILE, so seed them as one
        # (offset -> value) map each rather than a single shadow register.
        self._rf_file = {C._field_get(C.MT_RF_CSR_CFG_REG_ID, rf_csr) & 0x3F:
                         C._field_get(C.MT_RF_CSR_CFG_DATA, rf_csr)}
        self._bbp_file = {C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, bbp_csr) & 0xFF:
                          C._field_get(C.MT_BBP_CSR_CFG_VAL, bbp_csr)}
        self._rf_sel = C._field_get(C.MT_RF_CSR_CFG_REG_ID, rf_csr) & 0x3F
        self._bbp_sel = C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, bbp_csr) & 0xFF
        self._alc = alc
        self._sent_seq = 1

    def seed_rf(self, offset: int, value: int) -> None:
        self._rf_file[offset] = value

    def rr(self, offset: int) -> int:
        self.ops.append(("read", offset))
        if offset == C.MT_RF_CSR_CFG:
            return (C._field_prep(C.MT_RF_CSR_CFG_REG_ID, self._rf_sel)
                    | C._field_prep(C.MT_RF_CSR_CFG_DATA,
                                    self._rf_file.get(self._rf_sel, 0)))
        if offset == C.MT_BBP_CSR_CFG:
            return (C._field_prep(C.MT_BBP_CSR_CFG_REG_NUM, self._bbp_sel)
                    | C._field_prep(C.MT_BBP_CSR_CFG_VAL,
                                    self._bbp_file.get(self._bbp_sel, 0)))
        if offset == C.MT_TX_ALC_CFG_0:
            return self._alc
        return 0

    def wr(self, offset: int, val: int) -> None:
        self.ops.append(("wr", offset, val))
        # A store loads the addressed register; a fetch selects one and reads it
        # back. Either way the busy/kick bit clears once the transfer retires.
        if offset == C.MT_RF_CSR_CFG:
            reg = C._field_get(C.MT_RF_CSR_CFG_REG_ID, val)
            self._rf_sel = reg
            if val & C.MT_RF_CSR_CFG_WR:
                self._rf_file[reg] = C._field_get(C.MT_RF_CSR_CFG_DATA, val)
        elif offset == C.MT_BBP_CSR_CFG:
            reg = C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, val)
            self._bbp_sel = reg
            if val & C.MT_BBP_CSR_CFG_RW_MODE and not (val & C.MT_BBP_CSR_CFG_READ):
                self._bbp_file[reg] = C._field_get(C.MT_BBP_CSR_CFG_VAL, val)

    def rmw(self, offset: int, mask: int, val: int) -> int:
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        self.ops.append(("bulk", len(data)))
        self.payloads.append(data)
        self._sent_seq = C._field_get(C.MT_TXD_CMD_INFO_SEQ,
                                      int.from_bytes(data[:4], "little"))
        return len(data)

    bulk_out_inband_fw = bulk_out

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        """Echo the sent seq back, as the MCU does."""
        info = (C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, getattr(self, "_sent_seq", 1))
                | C._field_prep(C.MT_RXD_CMD_INFO_EVT_TYPE, C.CMD_DONE))
        return info.to_bytes(4, "little")

    def reset(self) -> None:
        self.ops = []


def make_phy(tp: FakeTransport, ee: MT7601UEepromParams | None = None):
    mcu = MT7601UMcu(tp)
    mcu.mcu_running = True
    return MT7601UPhy(tp, mcu, ee or MT7601UEepromParams())


def writes(tp: FakeTransport) -> list[tuple[int, int]]:
    """Every 32-bit register write the driver issued."""
    return [(o, v) for k, o, *rest in tp.ops if k == "wr" for v in [rest[0]]]


def data_writes(tp: FakeTransport) -> list[tuple[int, int]]:
    """Writes to a CSR that LOAD a register, excluding the read requests the
    driver issues into the same address (bbp_rr / rf_rr write before reading)."""
    out = []
    for off, val in writes(tp):
        if off == C.MT_BBP_CSR_CFG:
            # RW_MODE marks both directions; READ distinguishes a fetch from a store.
            if not (val & C.MT_BBP_CSR_CFG_RW_MODE) or (val & C.MT_BBP_CSR_CFG_READ):
                continue
        if off == C.MT_RF_CSR_CFG and not (val & C.MT_RF_CSR_CFG_WR):
            continue
        out.append((off, val))
    return out


def bulk_payloads(tp: FakeTransport) -> list[bytes]:
    return tp.payloads


def decode_pairs(payload: bytes) -> list[tuple[int, int]]:
    info = int.from_bytes(payload[:4], "little")
    ln = C._field_get(C.MT_TXD_INFO_LEN, info)
    body = payload[4:4 + ln]
    return [(int.from_bytes(body[j:j + 4], "little"),
             int.from_bytes(body[j + 4:j + 8], "little"))
            for j in range(0, len(body) - 7, 8)]


class TestRfWindow:
    def test_wr_encodes_value_bank_offset_and_kick(self) -> None:
        tp = FakeTransport()
        phy = make_phy(tp)
        phy.rf_wr(0, 4, 0x0A)
        val = [v for _o, v in data_writes(tp) if _o == C.MT_RF_CSR_CFG][0]
        assert C._field_get(C.MT_RF_CSR_CFG_DATA, val) == 0x0A
        assert C._field_get(C.MT_RF_CSR_CFG_REG_ID, val) == 4
        assert C._field_get(C.MT_RF_CSR_CFG_REG_BANK, val) == 0
        assert val & C.MT_RF_CSR_CFG_WR
        assert val & C.MT_RF_CSR_CFG_KICK

    def test_rr_asks_via_a_kick_only_write_then_reads(self) -> None:
        """phy.c:53 -- an RF read is a KICK-only write, not a plain register read."""
        tp = FakeTransport(rf_csr=C._field_prep(C.MT_RF_CSR_CFG_REG_ID, 4))
        phy = make_phy(tp)
        phy.rf_rr(0, 4)
        kick_write = [v for _o, v in writes(tp) if _o == C.MT_RF_CSR_CFG][0]
        assert not (kick_write & C.MT_RF_CSR_CFG_WR)      # read request
        assert kick_write & C.MT_RF_CSR_CFG_KICK

    def test_offset_above_63_rejected(self) -> None:
        phy = make_phy(FakeTransport())
        with pytest.raises(ValueError, match="out of range"):
            phy.rf_wr(0, 64, 0)

    def test_set_is_rmw_with_mask_zero(self) -> None:
        # rf_rr only extracts DATA when REG_ID/BANK match the request, so seed both.
        tp = FakeTransport(rf_csr=(C._field_prep(C.MT_RF_CSR_CFG_DATA, 0x0A)
                                   | C._field_prep(C.MT_RF_CSR_CFG_REG_ID, 4)))
        phy = make_phy(tp)
        out = phy.rf_set(0, 4, 1 << 7)
        assert out == 0x8A


class TestBbpWindow:
    def test_wr_sets_val_regnum_and_busy(self) -> None:
        tp = FakeTransport()
        phy = make_phy(tp)
        phy.bbp_wr(62, 0x37)
        val = [v for _o, v in data_writes(tp) if _o == C.MT_BBP_CSR_CFG][0]
        assert C._field_get(C.MT_BBP_CSR_CFG_VAL, val) == 0x37
        assert C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, val) == 62
        assert val & C.MT_BBP_CSR_CFG_BUSY
        assert val & C.MT_BBP_CSR_CFG_RW_MODE

    def test_rmc_skips_the_write_when_unchanged(self) -> None:
        cur = (C._field_prep(C.MT_BBP_CSR_CFG_VAL, 0x40)
               | C._field_prep(C.MT_BBP_CSR_CFG_REG_NUM, 4))
        tp = FakeTransport(bbp_csr=cur)             # bbp_rr needs REG_NUM to match
        phy = make_phy(tp)
        before = len(data_writes(tp))
        phy.bbp_rmc(4, 0x18, 0)                     # 20 MHz keeps the value as-is
        assert len(data_writes(tp)) == before

    def test_rmw_always_writes(self) -> None:
        tp = FakeTransport(bbp_csr=C._field_prep(C.MT_BBP_CSR_CFG_VAL, 0x40))
        phy = make_phy(tp)
        before = len(data_writes(tp))
        phy.bbp_rmw(4, 0x20, 0)
        assert len(data_writes(tp)) > before


class TestFreqPlan:
    def test_fourteen_rows(self) -> None:
        assert len(FREQ_PLAN) == 14

    @pytest.mark.parametrize("channel,expected", [
        (1, (0x99, 0x99, 0x09, 0x50)), (5, (0x46, 0x44, 0x08, 0x51)),
        (11, (0x46, 0x44, 0x08, 0x52)), (14, (0x33, 0x33, 0x0B, 0x52)),
    ])
    def test_rows_match_the_kernel_table(self, channel: int, expected) -> None:
        assert FREQ_PLAN[channel - 1] == expected

    def test_plan_is_written_as_rf_regs_17_to_20(self) -> None:
        tp = FakeTransport()
        phy = make_phy(tp)
        phy.set_channel(1)
        pairs = decode_pairs(bulk_payloads(tp)[0])
        assert [a & 0xFFFF for a, _v in pairs] == list(
            range(FREQ_PLAN_BASE_REG, FREQ_PLAN_BASE_REG + 4))
        assert all(a & ~0xFFFF == C.MT_MCU_MEMMAP_RF for a, _v in pairs)
        assert [v & 0xFF for _a, v in pairs] == list(FREQ_PLAN[0])

    def test_pairs_are_interleaved_address_value(self) -> None:
        """mcu.c:227-230 writes both into one skb in a single loop."""
        tp = FakeTransport()
        phy = make_phy(tp)
        phy.set_channel(1)
        pairs = decode_pairs(bulk_payloads(tp)[0])
        assert pairs[0][0] == (C.MT_MCU_MEMMAP_RF | FREQ_PLAN_BASE_REG)
        assert pairs[0][1] & 0xFF == FREQ_PLAN[0][0]


class TestBbpGain:
    def test_lna_gain_offsets_the_three_bbp_registers(self) -> None:
        ee = MT7601UEepromParams()
        ee.lna_gain = 0
        tp = FakeTransport()
        phy = make_phy(tp, ee)
        phy.set_channel(1)
        bbp_msg = bulk_payloads(tp)[1]
        pairs = decode_pairs(bbp_msg)
        assert [a & 0xFFFF for a, _v in pairs] == list(BBP_LNA_REGS)
        assert all(a & ~0xFFFF == C.MT_MCU_MEMMAP_BBP for a, _v in pairs)
        assert [v & 0xFF for _a, v in pairs] == [0x37, 0x37, 0x37]

    def test_lna_gain_of_two_lowers_the_value(self) -> None:
        ee = MT7601UEepromParams()
        ee.lna_gain = 2
        tp = FakeTransport()
        make_phy(tp, ee).set_channel(1)
        assert [v & 0xFF for _a, v in decode_pairs(bulk_payloads(tp)[1])] == [0x35] * 3


class TestChannelPower:
    def test_alc_write_uses_the_eeprom_per_channel_value(self) -> None:
        ee = MT7601UEepromParams()
        ee.chan_pwr = [7, 7, 7, 7, 9, 9, 9, 9, 13, 13, 13, 13, 13, 13]
        tp = FakeTransport(alc=0x2F2F0007)
        make_phy(tp, ee).set_channel(1)
        alc = [v for o, v in writes(tp) if o == C.MT_TX_ALC_CFG_0]
        assert C._field_get(0x3F3F, alc[0]) == 7

    def test_channel_five_uses_its_own_value(self) -> None:
        ee = MT7601UEepromParams()
        ee.chan_pwr = [7, 7, 7, 7, 9, 9, 9, 9, 13, 13, 13, 13, 13, 13]
        tp = FakeTransport(alc=0x2F2F0007)
        make_phy(tp, ee).set_channel(5)
        alc = [v for o, v in writes(tp) if o == C.MT_TX_ALC_CFG_0]
        assert C._field_get(0x3F3F, alc[0]) == 9

    @pytest.mark.parametrize("channel", [0, 15, 99])
    def test_out_of_range_channel_rejected(self, channel: int) -> None:
        with pytest.raises(ValueError, match="no channel"):
            make_phy(FakeTransport()).set_channel(channel)


class TestVcoCal:
    def test_two_writes_then_a_set(self) -> None:
        tp = FakeTransport()
        phy = make_phy(tp)
        # rf_set reads reg 4 back through the RF window, which only extracts DATA
        # when REG_ID matches, so seed the CSR accordingly.
        tp.seed_rf(4, 0x0A)                       # reg 4 already holds 0x0a
        tp.ops.clear()
        tp.payloads.clear()
        phy.vco_cal()
        regs = [(C._field_get(C.MT_RF_CSR_CFG_REG_ID, v), C._field_get(C.MT_RF_CSR_CFG_DATA, v))
                for o, v in data_writes(tp)]
        assert (4, 0x0A) in regs
        assert (5, 0x20) in regs
        assert regs[-1] == (4, 0x8A)               # rf_set(0, 4, BIT(7))


class TestCh14Fixup:
    def test_narrow_channels_take_the_wide_fixup_path(self) -> None:
        ee = MT7601UEepromParams()
        ee.real_cck_bw20 = [7, 6]
        tp = FakeTransport()
        phy = make_phy(tp, ee)
        phy.apply_ch14_fixup(1)
        assert ee.power_rate_table.cck[0].bw20 == 7
        assert ee.power_rate_table.cck[1].bw20 == 6

    def test_channel_14_applies_the_obw_fixup(self) -> None:
        ee = MT7601UEepromParams()
        ee.real_cck_bw20 = [7, 6]
        tp = FakeTransport()
        phy = make_phy(tp, ee)
        phy.apply_ch14_fixup(14)
        assert ee.power_rate_table.cck[0].bw20 == 5     # 7 - 2
        assert ee.power_rate_table.cck[1].bw20 == 4     # 6 - 2

    def test_ch14_path_also_writes_the_two_bbp_registers(self) -> None:
        tp = FakeTransport()
        make_phy(tp).apply_ch14_fixup(14)
        regs = [C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, v) for o, v in data_writes(tp)
                if o == C.MT_BBP_CSR_CFG]
        assert 4 in regs and BBP_CH14_REG in regs


class TestTxPwrCfg:
    def test_four_rate_values_pack_into_one_register(self) -> None:
        ee = MT7601UEepromParams()
        for rate in ee.power_rate_table.cck + ee.power_rate_table.ofdm:
            rate.bw20 = 0x05
        # apply_ch14_fixup restores the CCK rates from real_cck_bw20, so both have
        # to agree or the packed value reflects the fixup, not the table.
        ee.real_cck_bw20 = [0x05, 0x05]
        tp = FakeTransport()
        make_phy(tp, ee).set_channel(1)
        cfg = [v for o, v in writes(tp) if o == C.MT_TX_PWR_CFG_0]
        assert cfg == [0x05050505]

class TestAgc:
    """phy.c:948-968. The MAC initvals leave BBP 66 at a literal that is not what
    either dongle's LNA gain computes to."""

    def test_the_default_is_derived_from_the_eeprom_lna_gain(self) -> None:
        ee = MT7601UEepromParams()
        ee.lna_gain = 8
        assert make_phy(FakeTransport(), ee).agc_default() == 0x34
        ee.lna_gain = 0
        assert make_phy(FakeTransport(), ee).agc_default() == 0x24

    def test_the_default_wraps_as_a_u8(self) -> None:
        """phy.c:948 returns u8, so a large LNA gain wraps rather than overflowing."""
        ee = MT7601UEepromParams()
        ee.lna_gain = 127
        assert make_phy(FakeTransport(), ee).agc_default() == ((127 - 8) * 2 + 0x34) & 0xFF

    @staticmethod
    def _bbp66_writes(tp: FakeTransport) -> list[int]:
        return [C._field_get(C.MT_BBP_CSR_CFG_VAL, v)
                for o, v in data_writes(tp) if o == C.MT_BBP_CSR_CFG
                and C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, v) == 66]

    def test_a_scan_hop_resets_the_agc(self) -> None:
        ee = MT7601UEepromParams()
        ee.lna_gain = 8
        tp = FakeTransport()
        make_phy(tp, ee).set_channel(1, scan=True)
        assert self._bbp66_writes(tp) == [0x34]

    def test_a_deliberate_tune_leaves_the_agc_alone(self) -> None:
        """phy.c:434 gates the reset on MT7601U_STATE_SCANNING."""
        tp = FakeTransport()
        make_phy(tp).set_channel(1)
        assert self._bbp66_writes(tp) == []

    def test_save_is_idempotent_across_hops(self) -> None:
        """main.c:271 saves once at sw_scan_start, not per channel."""
        phy = make_phy(FakeTransport(bbp_csr=C._field_prep(C.MT_BBP_CSR_CFG_VAL, 0x14)))
        phy.agc_save()
        first = phy.agc_saved
        phy.agc_save()
        assert phy.agc_saved == first

    def test_restore_puts_the_pre_scan_value_back_once(self) -> None:
        tp = FakeTransport()
        phy = make_phy(tp)
        phy.agc_saved = 0x14
        phy.agc_restore()
        assert self._bbp66_writes(tp) == [0x14]
        phy.agc_restore()
        assert self._bbp66_writes(tp) == [0x14]      # nothing left to restore


class TestTxPwrSaturation:
    def test_an_out_of_range_rate_saturates_rather_than_wrapping(self) -> None:
        """phy.c:429 packs through int_to_s6 (eeprom.h:133), which clamps at -0x20.
        A bare 6-bit mask turns -34 into +30 and inverts the power setting."""
        ee = MT7601UEepromParams()
        for rate in ee.power_rate_table.cck + ee.power_rate_table.ofdm:
            rate.bw20 = -34
        ee.real_cck_bw20 = [-34, -34]
        tp = FakeTransport()
        make_phy(tp, ee).set_channel(1)
        cfg = [v for o, v in writes(tp) if o == C.MT_TX_PWR_CFG_0]
        assert cfg == [0x20202020]

    def test_an_in_range_rate_is_unchanged(self) -> None:
        ee = MT7601UEepromParams()
        for rate in ee.power_rate_table.cck + ee.power_rate_table.ofdm:
            rate.bw20 = -2
        ee.real_cck_bw20 = [-2, -2]
        tp = FakeTransport()
        make_phy(tp, ee).set_channel(1)
        cfg = [v for o, v in writes(tp) if o == C.MT_TX_PWR_CFG_0]
        assert cfg == [0x3E3E3E3E]
