"""Hardware-free regression for the RTL8188EUS (DKMS) IOL engine + efuse patch.

Full byte-for-byte replay (incl. the watchdog-filtered stream) lives in
``scripts/chips/rtl8188eus_dkms/verify_pcap.py``.
"""
import pytest

from wifit3.chips.rtl8188eus_dkms import efuse
from wifit3.chips.rtl8188eus_dkms.constants import (
    CMD_READ_EFUSE_MAP,
    DEFAULT_CRYSTAL_CAP,
    EFUSE_ACCESS_OFF,
    REG_EFUSE_ACCESS,
    REG_EFUSE_CTRL,
    REG_HMEBOX_E0,
    REG_PKT_BUFF_ACCESS_CTRL,
    REG_PKTBUF_DBG_DATA_L,
    REG_SYS_CFG,
    REG_TDECTRL,
    REG_TXPKTBUF_DBG,
    SW_OFFLOAD_EN,
    TXPKT_BUF_SELECT,
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
    logical = efuse._phymap_to_logical(phymap, bytes(512))
    assert logical[0:8] == bytes([0, 1, 2, 3, 4, 5, 6, 7])
    assert logical[8] == 0xFF                     # untouched section stays 0xFF


def test_phymap_to_logical_extended_header_crystal_cap():
    # Extended header [0xEF, 0x20] -> offset 23 (section for logical 0xB8..0xBF),
    # all words; data byte 1 (high of word0) lands at logical[0xB9] = crystal_cap.
    phymap = bytes([0xEF, 0x20, 0x00, 0x20, 0, 0, 0, 0, 0, 0, 0xFF])
    logical = efuse._phymap_to_logical(phymap, bytes(512))
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


def test_parse_tx_power_blank_bases_use_8188e_ic_defaults():
    p = efuse._parse_tx_power(b"\xff" * 512)
    assert p.cck_base == (0x2D,) * 6
    assert p.bw40_base == (0x2D,) * 5
    assert (p.cck_diff, p.ofdm_diff, p.bw20_diff) == (0, -1, -1)


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


def test_channel_plan_hardware_forced_fcc_limits_to_1_through_11():
    m = bytearray(b"\xff" * 512)
    m[0] = 0x29
    m[1] = 0x81
    m[0xB8] = 0xA2  # EEPROM_CHANNEL_PLAN_BY_HW_MASK | RTW_CHPLAN_FCC1_NULL
    plan, channels = efuse.read_channel_plan(bytes(m), autoload_failed=False)
    assert plan == 0x22
    assert channels == tuple(range(1, 12))


def test_channel_plan_autoload_failure_uses_world_default():
    plan, channels = efuse.read_channel_plan(b"\xff" * 512, autoload_failed=True)
    assert plan == 0x20
    assert channels == tuple(range(1, 14))


def test_channel_plan_country_code_overrides_hardware_plan():
    m = bytearray(b"\xff" * 512)
    m[0xB8] = 0xA2
    m[0xCB:0xCD] = b"JP"
    plan, channels = efuse.read_channel_plan(bytes(m), autoload_failed=False)
    assert plan == 0xA2
    assert channels == tuple(range(1, 15))


def test_iol_efuse_patch_sequence():
    t = Tx(queues={REG_SYS_CFG: [0x37, 0xB7],
                   REG_HMEBOX_E0: [0x00, 0x00, 0x00,   # READ_EFUSE_MAP: init, poll-clear, status
                                   0x00, 0x00, 0x00]})  # EFUSE_PATCH: init, poll-clear, status
    assert efuse.iol_efuse_patch(t) is True
    # SW_OFFLOAD_EN on then off; two control writes (0x02 then 0x04).
    assert t.writes[0] == (REG_SYS_CFG, 0xB7)
    assert (REG_HMEBOX_E0, 0x02) in t.writes and (REG_HMEBOX_E0, 0x04) in t.writes
    assert t.writes[-1] == (REG_SYS_CFG, 0x37)


class Wire:
    """Logs every op in order; serves the IOL mailbox, the PKTBUF debug port and a
    physical efuse behind REG_EFUSE_CTRL."""

    def __init__(self, hmebox_e0=0x00, physical_efuse=b""):
        self.ops = []
        self._hmebox_e0 = hmebox_e0
        self._physical_efuse = physical_efuse
        self._efuse_addr = 0

    def read8(self, a):
        self.ops.append(("r8", a))
        if a == REG_HMEBOX_E0:
            return self._hmebox_e0
        if a == REG_TXPKTBUF_DBG:
            return 0x01
        return 0x00

    def read16(self, a):
        self.ops.append(("r16", a))
        return 0x0000

    def read32(self, a):
        self.ops.append(("r32", a))
        if a == REG_PKTBUF_DBG_DATA_L:
            return 0xFFFF0004                           # length 4 -> 2 map bytes, 0xFF 0xFF
        if a == REG_EFUSE_CTRL:
            if self._efuse_addr < len(self._physical_efuse):
                return 0x80000000 | self._physical_efuse[self._efuse_addr]
            return 0x800000FF
        return 0

    def write8(self, a, v):
        self.ops.append(("w8", a, v & 0xFF))
        if a == REG_EFUSE_CTRL + 1:
            self._efuse_addr = (self._efuse_addr & 0x300) | (v & 0xFF)
        elif a == REG_EFUSE_CTRL + 2:
            self._efuse_addr = (self._efuse_addr & 0xFF) | ((v & 0x03) << 8)

    def write16(self, a, v):
        self.ops.append(("w16", a, v & 0xFFFF))

    def efuse_byte_addrs(self):
        return [op[2] for op in self.ops if op[:2] == ("w8", REG_EFUSE_CTRL + 1)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(efuse.time, "sleep", lambda s: None)


@pytest.fixture
def power_on_logged(monkeypatch):
    monkeypatch.setattr(efuse, "power_on", lambda t: t.ops.append(("power_on",)))


def test_read_chip_params_iol_success_wire_ops_unchanged(power_on_logged):
    t = Wire()
    efuse.read_chip_params(t)
    expected = Wire()
    efuse.hal_efuse_power_switch(expected, True)
    expected.ops.append(("power_on",))
    efuse.iol_mode_enable(expected, True, fw_ready=False)
    expected.write8(REG_TDECTRL + 1, 0)
    expected.write8(REG_PKT_BUFF_ACCESS_CTRL, TXPKT_BUF_SELECT)
    efuse.iol_execute(expected, CMD_READ_EFUSE_MAP)
    efuse._read_phymap_from_txpktbuf(expected, 0)
    efuse.iol_mode_enable(expected, False)
    expected.write8(REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)
    assert t.ops == expected.ops
    assert t.efuse_byte_addrs() == []


def test_read_chip_params_iol_failure_falls_back_to_byte_reads(power_on_logged):
    # Section 0 all words (EEPROM ID 0x8129 + 6 bytes), then ext header -> section 23
    # word 0 only (logical 0xB8/0xB9 = channel plan, crystal cap).
    physical = bytes([0x00, 0x29, 0x81, 2, 3, 4, 5, 6, 7,
                      0xEF, 0x2E, 0x20, 0x21, 0xFF])
    t = Wire(hmebox_e0=0x20, physical_efuse=physical)     # READ_EFUSE_MAP error bit
    p = efuse.read_chip_params(t)
    assert p.efuse_map == efuse._phymap_to_logical(physical, bytes(512))
    assert p.eeprom_id_valid and p.crystal_cap == 0x21
    assert t.efuse_byte_addrs() == list(range(len(physical)))
    assert ("r32", REG_PKTBUF_DBG_DATA_L) not in t.ops          # no PKTBUF readback
    first_byte_read = t.ops.index(("w8", REG_EFUSE_CTRL + 1, 0))
    assert t.ops[first_byte_read - 1][:2] == ("w8", REG_SYS_CFG)  # after iol_mode_enable(0)
    assert t.ops[-1] == ("w8", REG_EFUSE_ACCESS, EFUSE_ACCESS_OFF)


def test_read_chip_params_empty_efuse_leaves_zeroed_map(power_on_logged):
    # Both decoders leave efuse_eeprom_data (zeroed HalData) untouched on an empty efuse;
    # rtw_mask_map_read then sets only the masked bytes to 0xFF.
    for hmebox_e0 in (0x00, 0x20):                          # IOL success, IOL failure
        p = efuse.read_chip_params(Wire(hmebox_e0=hmebox_e0, physical_efuse=b"\xFF"))
        assert p.efuse_map == efuse.rtw_mask_map_read(bytes(512))
        assert p.autoload_failed and not p.eeprom_id_valid
        assert p.crystal_cap == DEFAULT_CRYSTAL_CAP and p.thermal_meter == 0x18
        assert p.tx_power.cck_base == (0,) * 6 and p.tx_power.bw40_base == (0,) * 5


def test_read_chip_params_autoload_failed_skips_efuse_read(power_on_logged):
    t = Wire()
    p = efuse.read_chip_params(t, autoload_failed=True)
    assert t.ops == []
    assert p.efuse_map == b"\xFF" * 512 and p.autoload_failed


def test_read_efuse_byte_never_touches_efuse_test():
    # ReadEFuseByte's 0x34[11] clear is 8723B-only; the I-cut clear is in efuse_OneByteRead.
    t = Wire(physical_efuse=bytes([0x5A]))
    assert efuse.read_efuse_byte(t, 0) == 0x5A
    assert not [op for op in t.ops if op[1] in (0x34, 0x35)]


def test_hal_efuse_read_efuse_88e_empty_efuse_reads_one_byte():
    t = Wire(physical_efuse=bytes([0xFF]))
    pbuf = bytes(512)
    assert efuse.hal_efuse_read_efuse_88e(t, pbuf) is pbuf
    assert t.efuse_byte_addrs() == [0]


def test_hal_efuse_read_efuse_88e_bad_offset_skips_without_wren_shift():
    # Ext header [0xEF, 0xF0]: offset 0x7F >= 64, wren 0 -> skip 8 bytes.
    physical = bytes([0xEF, 0xF0]) + bytes(8) + bytes([0x3E, 0xAA, 0xBB, 0xFF])
    t = Wire(physical_efuse=physical)
    m = efuse.hal_efuse_read_efuse_88e(t, bytes(512))
    assert m[3 * 8:3 * 8 + 2] == bytes([0xAA, 0xBB])        # section 3, word 0
    assert t.efuse_byte_addrs() == [0, 1, 10, 11, 12, 13]


def test_phymap_to_logical_bad_offset_skips_nothing():
    # efuse_phymap_to_logical has no bad-offset branch: the next byte is the next header.
    physical = bytes([0xEF, 0xF0, 0x3E, 0xAA, 0xBB, 0xFF])
    m = efuse._phymap_to_logical(physical, bytes(512))
    assert m[3 * 8:3 * 8 + 2] == bytes([0xAA, 0xBB])        # section 3, word 0


def test_phymap_to_logical_empty_returns_pbuf():
    pbuf = bytes(512)
    assert efuse._phymap_to_logical(b"\xFF\x00", pbuf) is pbuf


def _offsets(lo, hi):
    return set(range(lo, hi + 1))


def test_efuse_is_masked_8188e_usb_offsets():
    # Array_MP_8188E_MUSB, HalEfuseMask8188E_USB.c:28: a clear bit hides its 2-byte word.
    hidden = {i for i in range(512) if efuse.efuse_is_masked(i)}
    assert hidden == (_offsets(0x1C, 0xB7) | _offsets(0xCA, 0xCF)
                      | _offsets(0x100, 0x12F) | _offsets(0x13E, 0x1FF))


def test_read_chip_params_masks_board_option_and_country_code(power_on_logged):
    # Section 0 (EEPROM ID), then ext header -> section 25 words 1-2 (0xCA = ePA+eLNA, 0xCB "JP").
    physical = bytes([0x00, 0x29, 0x81, 2, 3, 4, 5, 6, 7,
                      0x2F, 0x39, 0x00, 0x4A, 0x50, 0x00, 0xFF])
    p = efuse.read_chip_params(Wire(hmebox_e0=0x20, physical_efuse=physical))
    assert efuse._phymap_to_logical(physical, bytes(512))[0xCA:0xCD] == b"\x00JP"
    assert p.efuse_map[0xCA:0xCD] == b"\xFF\xFF\xFF"
    assert (p.board.external_pa_2g, p.board.external_lna_2g, p.board.type_glna) == (False, False, 0)
