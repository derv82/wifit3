"""RTL8822CU per hop TX AGC flush, driven by the computed TX power index. No hardware.

``_set_tx_power`` is the only consumer of the TX power port, so this is where the computation meets
the wire. The recorded reference indices and diff dwords in ``recorded_txagc`` came off a pcap of
the vendor driver; the values here come from that adapter's EFUSE PG bytes through the ported C.
"""
import pytest

from wifit3.chips.rtl8822cu import phy
from wifit3.chips.rtl8822cu.efuse import EfuseInfo, hal_rfpath_init
from wifit3.chips.rtl8822cu.txpwr_index import txpwr_idx_state

from . import recorded_txagc
from .recorded_txagc import DIFF_GROUP_DWORD, SECTION_REF_2G, SECTION_REF_5G_OFDM

PG_SADDR = 0x10
# The recorded D-Link AC13U PG TX power region, logical 0x10..0x63. Only this region.
PG_RECORDED = bytes.fromhex(
    "49484b494c483f3f424243000000ffffffff"
    "484849494a4b4a474444454646450000ffff00ff0000ffff"
    "565858585856464748494a000000ffffffff"
    "494a4b4a484847444342424543460000ffff00ff0000ffff"
)
EEPROM_RF_BOARD_OPTION_RECORDED = 0x01
TPT_MODE_THERMAL, TPT_MODE_TSSI = 0x00, 0x40

VHT_1SS_MCS8, VHT_1SS_MCS9 = 0x34, 0x35     # DESC_RATE [SRC include/hal_com.h:33-120]
VHT_2SS_MCS8, VHT_2SS_MCS9 = 0x3E, 0x3F
DESC_RATEMCS7 = 0x13


class RecordingTransport:
    """A BB register file that answers every read with 0, so _bbrstb_txagc_off stays quiet and the
    recorded writes are exactly the TX AGC flush."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []

    def read32(self, address: int) -> int:
        return 0

    def write32(self, address: int, value: int) -> None:
        self.writes.append((address, value))


def _efuse(*, tpt_byte: int = TPT_MODE_THERMAL) -> EfuseInfo:
    # 0xC9 stays 0xFF. The recorded device has 0x4F; both are outside the accepted
    # trx_path_bmp set [SRC hal/rtl8822c/rtl8822c_ops.c:528-534], so eeprom_trx_path_bmp
    # is 0 either way.
    logical = bytearray(b"\xff" * 768)
    logical[0xC1] = EEPROM_RF_BOARD_OPTION_RECORDED
    logical[0xC8] = tpt_byte
    logical[PG_SADDR:PG_SADDR + len(PG_RECORDED)] = PG_RECORDED
    return EfuseInfo(True, True, bytes(logical), b"\xff" * 512)


def _state(*, tpt_byte: int = TPT_MODE_THERMAL):
    efuse = _efuse(tpt_byte=tpt_byte)
    return txpwr_idx_state(efuse, hal_rfpath_init(efuse, ant_num=2, hw_stype=0x00, rf_2t2r=True))


def _refs(writes: list[tuple[int, int]]) -> dict[int, int]:
    """The four reference registers, by address, decoded out of their masked field."""
    out = {}
    for address, value in writes:
        if address in (0x18A0, 0x41A0):
            out[address] = (value & 0x007F0000) >> 16
        elif address in (0x18E8, 0x41E8):
            out[address] = (value & 0x0001FC00) >> 10
    return out


def _diff_dwords(writes: list[tuple[int, int]]) -> dict[int, int]:
    return {address - 0x3A00: value for address, value in writes if 0x3A00 <= address <= 0x3A3C}


def _tune(transport, channel: int, state) -> None:
    phy._set_tx_power(transport, channel, state)


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G))
def test_a_two_point_four_gigahertz_tune_writes_the_recorded_references(channel):
    """The computed CCK and OFDM reference indices, on both paths, against the wire's."""
    transport = RecordingTransport()
    _tune(transport, channel, _state())
    cck_a, cck_b, ofdm_a, ofdm_b = SECTION_REF_2G[channel]
    assert _refs(transport.writes) == {0x18A0: cck_a, 0x41A0: cck_b,
                                       0x18E8: ofdm_a, 0x41E8: ofdm_b}


@pytest.mark.parametrize("channel", sorted(SECTION_REF_5G_OFDM))
def test_a_five_gigahertz_tune_writes_the_recorded_ofdm_reference(channel):
    """5 GHz runs no CCK section, so the CCK references carry the previous 2.4 GHz tune forward.
    [SRC hal/rtl8822c/rtl8822c_phy.c:662]"""
    transport = RecordingTransport()
    state = _state()
    _tune(transport, 1, state)
    transport.writes.clear()
    _tune(transport, channel, state)
    cck_a, cck_b, _, _ = SECTION_REF_2G[1]
    ofdm_a, ofdm_b = SECTION_REF_5G_OFDM[channel]
    assert _refs(transport.writes) == {0x18A0: cck_a, 0x41A0: cck_b,
                                       0x18E8: ofdm_a, 0x41E8: ofdm_b}


@pytest.mark.parametrize("channel", sorted(SECTION_REF_2G) + sorted(SECTION_REF_5G_OFDM))
def test_every_tune_writes_the_recorded_power_by_rate_diff_table(channel):
    """The twelve MIN over paths dwords config_phydm_write_txagc_diff_8822c puts at 0x3A00, which
    are channel independent because the by rate curve is. [SRC phydm_hal_api8822c.c:522-580]"""
    transport = RecordingTransport()
    state = _state()
    _tune(transport, 1, state)
    transport.writes.clear()
    _tune(transport, channel, state)
    assert _diff_dwords(transport.writes) == DIFF_GROUP_DWORD


def test_the_negative_by_rate_offsets_are_stored_as_the_signed_tx_gain_index():
    """hal_com_get_txpwr_idx clamps to 0..txgi_max before the diff is taken
    [SRC hal/hal_com_phycfg.c:6341-6344], so txagc_buff holds a real index, not the 7 bit wire
    encoding. The four VHT MCS8/MCS9 rates are the only ones whose by rate offset is negative."""
    transport = RecordingTransport()
    _tune(transport, 1, _state())
    buff = transport._txagc_buff_state
    for path in (0, 1):
        ofdm_ref = buff[path][DESC_RATEMCS7]
        assert buff[path][VHT_1SS_MCS8] == ofdm_ref - 4
        assert buff[path][VHT_1SS_MCS9] == ofdm_ref - 8
        assert buff[path][VHT_2SS_MCS8] == ofdm_ref - 4
        assert buff[path][VHT_2SS_MCS9] == ofdm_ref - 8


def test_a_tssi_part_writes_no_tx_power_at_all():
    """EFUSE 0xC8[7:4] >= 4 selects the TSSI codeword arm [SRC hal/hal_com_phycfg.c:6312-6333],
    which is not ported: nothing is written rather than a fabricated index."""
    transport = RecordingTransport()
    assert _state(tpt_byte=TPT_MODE_TSSI) is None
    _tune(transport, 1, None)
    assert transport.writes == []


def test_the_recorded_oracle_is_unedited():
    """recorded_txagc holds observations, not derivable values, and four test modules measure
    themselves against it. An edit must fail here rather than quietly redefine what is
    correct for every comparison that hangs off it."""
    assert {name: len(getattr(recorded_txagc, name))
            for name in recorded_txagc.ENTRY_COUNTS} == recorded_txagc.ENTRY_COUNTS
    assert recorded_txagc.digest() == recorded_txagc.DIGEST
