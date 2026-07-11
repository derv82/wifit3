"""Hardware-free regression for the RTL8188EUS (DKMS) IOL engine + efuse patch.

Full byte-for-byte replay (incl. the watchdog-filtered stream) lives in
``scripts/rtl8188eus_dkms/verify_pcap.py``.
"""
from wifit3.chips.rtl8188eus_dkms import efuse
from wifit3.chips.rtl8188eus_dkms.constants import (
    CMD_READ_EFUSE_MAP,
    REG_HMEBOX_E0,
    REG_SYS_CFG,
    SW_OFFLOAD_EN,
)


class Tx:
    """Serves a per-address read queue; records writes."""

    def __init__(self, queues=None):
        self.writes = []
        self._q = {a: list(v) for a, v in (queues or {}).items()}
        self._last = {}

    def read8(self, a):
        q = self._q.get(a)
        if q:
            self._last[a] = q.pop(0)
        return self._last.get(a, 0x00)

    def write8(self, a, v):
        self.writes.append((a, v & 0xFF))
        self._last[a] = v & 0xFF


def test_iol_mode_enable_toggles_sw_offload():
    t = Tx(queues={REG_SYS_CFG: [0x37]})
    efuse.iol_mode_enable(t, True)
    assert t.writes == [(REG_SYS_CFG, 0x37 | SW_OFFLOAD_EN)]   # 0xB7
    t = Tx(queues={REG_SYS_CFG: [0xB7]})
    efuse.iol_mode_enable(t, False)
    assert t.writes == [(REG_SYS_CFG, 0xB7 & ~SW_OFFLOAD_EN)]  # 0x37


def test_iol_execute_polls_until_control_clears():
    # initial read, then poll: control-set twice, then clear, then final status read.
    t = Tx(queues={REG_HMEBOX_E0: [0x00, 0x02, 0x02, 0x00, 0x00]})
    assert efuse.iol_execute(t, CMD_READ_EFUSE_MAP) is True
    # wrote control onto the initial value (0x00 | 0x02).
    assert t.writes == [(REG_HMEBOX_E0, 0x02)]


def test_iol_execute_reports_error_bit():
    # control clears but the matching <<4 error bit (0x20 for 0x02) is set -> FAIL.
    t = Tx(queues={REG_HMEBOX_E0: [0x00, 0x00, 0x20]})
    assert efuse.iol_execute(t, CMD_READ_EFUSE_MAP) is False


def test_phymap_to_logical_non_extended():
    # Section 0, word-enable 0 (all 4 words) + 8 data bytes -> logical[0:8].
    phymap = bytes([0x00, 0, 1, 2, 3, 4, 5, 6, 7, 0xFF])
    logical = efuse._phymap_to_logical(phymap)
    assert logical[0:8] == bytes([0, 1, 2, 3, 4, 5, 6, 7])
    assert logical[8] == 0xFF                     # untouched section stays 0xFF


def test_phymap_to_logical_extended_header_crystal_cap():
    # Extended header [0xEF, 0x20] -> offset 23 (section for logical 0xB8..0xBF),
    # all words; data byte 1 (high of word0) lands at logical[0xB9] = crystal_cap.
    phymap = bytes([0xEF, 0x20, 0x00, 0x20, 0, 0, 0, 0, 0, 0, 0xFF])
    logical = efuse._phymap_to_logical(phymap)
    assert logical[0xB8] == 0x00 and logical[0xB9] == 0x20


def test_parse_tx_power_2g():
    # Path-A PG block at 0x10 from capture-1: 6 CCK base, 5 BW40 base, diff byte 0x01.
    m = bytearray(b"\xFF" * 512)
    m[0x10:0x10 + 12] = bytes([0x30, 0x30, 0x2F, 0x2E, 0x2E, 0x2E,     # CCK base
                               0x33, 0x33, 0x33, 0x32, 0x31,            # BW40 base
                               0x01])                                    # diff: BW20=0 OFDM=1
    p = efuse._parse_tx_power(bytes(m))
    assert p.cck_base == (0x30, 0x30, 0x2F, 0x2E, 0x2E, 0x2E)
    assert p.bw40_base == (0x33, 0x33, 0x33, 0x32, 0x31)
    assert (p.cck_diff, p.ofdm_diff, p.bw20_diff) == (0, 1, 0)


def test_parse_tx_power_signed_diff_nibbles():
    # diff byte 0xF8 -> MSB nibble 0xF = -1 (BW20), LSB nibble 0x8 = -8 (OFDM).
    m = bytearray(b"\x20" * 512)
    m[0x10 + 11] = 0xF8
    p = efuse._parse_tx_power(bytes(m))
    assert (p.bw20_diff, p.ofdm_diff) == (-1, -8)


def _board(rfe: int):
    m = bytearray(b"\xFF" * 512)
    m[0xCA] = rfe
    return efuse.read_board_options(bytes(m))


def test_board_options_internal_pa_lna_reference():
    # This dev card's 0xCA is blank (0xFF) -> [3:2]=3 iPA+iLNA, [6:4]=7 -> TypeGLNA 0;
    # a programmed [3:2]==3 (0x0C, 0x0F) is also internal.
    for rfe in (0xFF, 0x0C, 0x0F):
        b = _board(rfe)
        assert (b.external_pa_2g, b.external_lna_2g) == (False, False)


def test_board_options_external_pa_lna_decoded():
    # 0xCA[3:2]: 0=ePA+eLNA, 1=ePA+iLNA, 2=iPA+eLNA (external PA and/or LNA).
    assert (_board(0x00).external_pa_2g, _board(0x00).external_lna_2g) == (True, True)
    assert (_board(0x04).external_pa_2g, _board(0x04).external_lna_2g) == (True, False)
    assert (_board(0x08).external_pa_2g, _board(0x08).external_lna_2g) == (False, True)


def test_board_options_type_glna_gain_select():
    # 0xCA[6:4]: 0->0x1 (10dB), 2->0x2 (14dB), everything else -> 0x0. Keep [3:2]=2
    # (ext LNA) so the byte is a realistic ext-LNA burn.
    assert _board(0b0000_1000).type_glna == 0x1   # [6:4]=0
    assert _board(0b0010_1000).type_glna == 0x2   # [6:4]=2
    assert _board(0b0001_1000).type_glna == 0x0   # [6:4]=1 (unsupported)
    assert _board(0xFF).type_glna == 0x0          # [6:4]=7 -> reference card


def test_iol_efuse_patch_sequence():
    t = Tx(queues={REG_SYS_CFG: [0x37, 0xB7],
                   REG_HMEBOX_E0: [0x00, 0x00, 0x00,   # READ_EFUSE_MAP: init, poll-clear, status
                                   0x00, 0x00, 0x00]})  # EFUSE_PATCH: init, poll-clear, status
    assert efuse.iol_efuse_patch(t) is True
    # SW_OFFLOAD_EN on then off; two control writes (0x02 then 0x04).
    assert t.writes[0] == (REG_SYS_CFG, 0xB7)
    assert (REG_HMEBOX_E0, 0x02) in t.writes and (REG_HMEBOX_E0, 0x04) in t.writes
    assert t.writes[-1] == (REG_SYS_CFG, 0x37)
