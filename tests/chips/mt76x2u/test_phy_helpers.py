"""mt76x2u phy.py kernel helpers.

Coverage targets:
  - rate-power math (`_tx_power_mask`, `_get_max_rate_power`, `_get_min_rate_power`,
    `_add_rate_power_offset`, `_limit_rate_power`)
  - `phy_set_txpower_low` register writes (TX_ALC_CFG_0 RMW + 8 TX_PWR_CFG writes)
  - `init_agc_gain` register read decoding
  - `adjust_vga_gain` false-CCA driven step logic
  - `update_channel_gain` state machine — gain_change branch + adjust-only branch
"""
from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import phy


def _empty_rate_power() -> dict:
    return {
        "cck": [0, 0, 0, 0],
        "ofdm": [0, 0, 0, 0, 0, 0, 0, 0],
        "ht": [0] * 16,
        "vht": [0, 0],
    }


class FakeTransport:
    """Records writes; serves reads from a backing dict (default 0)."""

    def __init__(self, reads: dict[int, int] | None = None):
        self.reads = dict(reads or {})
        self.writes: list[tuple[int, int]] = []
        self.rmws: list[tuple[int, int, int]] = []

    def read32(self, addr: int) -> int:
        return self.reads.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))

    def rmw32(self, addr: int, mask: int, value: int) -> None:
        cur = self.reads.get(addr, 0)
        new = ((cur & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.reads[addr] = new
        self.rmws.append((addr, mask, value & 0xFFFFFFFF))
        self.rmws.append((addr, mask, value))


# ---------------------------------------------------------------------------
# Rate-power math
# ---------------------------------------------------------------------------

def test_tx_power_mask_packs_four_6bit_values():
    # Each 6-bit value goes into bits N*8 + [0..5]. The high 2 bits of each
    # byte stay zero (kernel masks with (BIT(6)-1) = 0x3F).
    val = phy._tx_power_mask(0x3F, 0x21, 0x12, 0x05)
    assert (val >> 0) & 0xFF == 0x3F
    assert (val >> 8) & 0xFF == 0x21
    assert (val >> 16) & 0xFF == 0x12
    assert (val >> 24) & 0xFF == 0x05


def test_tx_power_mask_clamps_high_bits():
    """Values >6 bits get silently truncated (low 6 bits kept)."""
    val = phy._tx_power_mask(0xFF, 0xFF, 0xFF, 0xFF)
    for i in range(4):
        assert (val >> (i * 8)) & 0xFF == 0x3F


def test_add_rate_power_offset_mutates_all_slots():
    rp = _empty_rate_power()
    rp["cck"][0] = 1
    rp["ht"][5] = 10
    phy._add_rate_power_offset(rp, 3)
    assert rp["cck"][0] == 4
    assert rp["cck"][1] == 3
    assert rp["ht"][5] == 13
    assert rp["vht"][1] == 3


def test_limit_rate_power_clamps_per_slot():
    rp = _empty_rate_power()
    rp["cck"] = [10, 20, 30, 5]
    phy._limit_rate_power(rp, 15)
    assert rp["cck"] == [10, 15, 15, 5]


def test_get_max_rate_power_picks_global_max():
    rp = _empty_rate_power()
    rp["cck"] = [1, 2, 3, 4]
    rp["ofdm"] = [5, 6, 7, 8, 0, 0, 0, 0]
    rp["ht"][7] = 50
    assert phy._get_max_rate_power(rp) == 50


def test_get_min_rate_power_skips_zero_slots():
    """Kernel min: ignore 0 entries (= "rate not in table")."""
    rp = _empty_rate_power()
    rp["cck"] = [0, 0, 0, 0]   # all zero
    rp["ofdm"] = [0, 7, 0, 3, 0, 0, 0, 0]
    rp["ht"][3] = 5
    # The non-zero values are 7, 3, 5 → min = 3.
    assert phy._get_min_rate_power(rp) == 3


def test_get_min_rate_power_all_zero_returns_zero():
    rp = _empty_rate_power()
    assert phy._get_min_rate_power(rp) == 0


# ---------------------------------------------------------------------------
# phy_set_txpower_low — register layout
# ---------------------------------------------------------------------------

def test_phy_set_txpower_low_writes_alc_cfg_0_and_nine_pwr_cfgs():
    t = FakeTransport()
    rp = _empty_rate_power()
    rp["cck"] = [0x11, 0, 0x12, 0]
    rp["ofdm"] = [0x13, 0, 0x14, 0, 0x15, 0, 0x16, 0]
    rp["ht"] = [0x20, 0, 0x21, 0, 0x22, 0, 0x23, 0,
                0x24, 0, 0x25, 0, 0x26, 0, 0x27, 0]
    rp["vht"] = [0x30, 0x31]
    phy.phy_set_txpower_low(t, rp, txp_0=0x1A, txp_1=0x05)

    addrs = [addr for addr, _ in t.writes]
    rmw_addrs = [addr for addr, _, _ in t.rmws]
    assert C.MT_TX_ALC_CFG_0 in rmw_addrs   # CH_INIT_0/1 set via two rmw_field ops
    for pwr_reg in (
        C.MT_TX_PWR_CFG_0, C.MT_TX_PWR_CFG_1, C.MT_TX_PWR_CFG_2,
        C.MT_TX_PWR_CFG_3, C.MT_TX_PWR_CFG_4, C.MT_TX_PWR_CFG_7,
        C.MT_TX_PWR_CFG_8, C.MT_TX_PWR_CFG_9,
    ):
        assert pwr_reg in addrs, f"missing write to 0x{pwr_reg:04x}"


def test_phy_set_txpower_low_alc_cfg_0_carries_txp_fields():
    """ALC_CFG_0 is two rmw_field ops: CH_INIT_0 in bits 0..5, CH_INIT_1 in 8..13."""
    t = FakeTransport()
    rp = _empty_rate_power()
    phy.phy_set_txpower_low(t, rp, txp_0=0x05, txp_1=0x1A)
    val = t.reads[C.MT_TX_ALC_CFG_0]   # final register value after both rmws
    assert (val & C.MT_TX_ALC_CFG_0_CH_INIT_0_MASK) == 0x05
    assert (val & C.MT_TX_ALC_CFG_0_CH_INIT_1_MASK) >> 8 == 0x1A


# ---------------------------------------------------------------------------
# init_agc_gain — decoded BBP AGC 8/9 field
# ---------------------------------------------------------------------------

def test_init_agc_gain_extracts_gain_field():
    """AGC_GAIN field is bits 14:8 of MT_BBP_AGC_R8/R9. Field value 0x40 →
    bit pattern 0x40 << 8 = 0x4000 in the register."""
    t = FakeTransport({
        C.MT_BBP_AGC_R8: 0x0000_40FF,   # bits 14:8 = 0x40
        C.MT_BBP_AGC_R9: 0x0000_05FF,   # bits 14:8 = 0x05
    })
    g0, g1 = phy.init_agc_gain(t)
    assert g0 == 0x40
    assert g1 == 0x05


# ---------------------------------------------------------------------------
# adjust_vga_gain — false-CCA-driven step
# ---------------------------------------------------------------------------

def test_adjust_vga_gain_increments_on_high_cca():
    """false_cca > 800 + adjust < limit → +2."""
    t = FakeTransport({C.MT_RX_STAT_1: 1000})    # > 800
    new, changed, lowest = phy.adjust_vga_gain(t, low_gain=0, agc_gain_adjust=0)
    assert changed is True
    assert new == 2


def test_adjust_vga_gain_decrements_on_low_cca():
    """false_cca < 10 + adjust > 0 → -2."""
    t = FakeTransport({C.MT_RX_STAT_1: 5})
    new, changed, lowest = phy.adjust_vga_gain(t, low_gain=0, agc_gain_adjust=4)
    assert changed is True
    assert new == 2


def test_adjust_vga_gain_no_change_in_middle():
    """false_cca in [10, 800] + adjust > 0 → unchanged."""
    t = FakeTransport({C.MT_RX_STAT_1: 100})
    new, changed, lowest = phy.adjust_vga_gain(t, low_gain=0, agc_gain_adjust=2)
    assert changed is False
    assert new == 2


def test_adjust_vga_gain_lowest_when_at_limit():
    """low_gain > 0 raises the limit to 16. At limit → agc_lowest_gain True."""
    t = FakeTransport({C.MT_RX_STAT_1: 600})    # avoids decrement
    new, _, lowest = phy.adjust_vga_gain(t, low_gain=1, agc_gain_adjust=16)
    assert lowest is True
    assert new == 16


# ---------------------------------------------------------------------------
# update_channel_gain — state machine
# ---------------------------------------------------------------------------

def test_update_channel_gain_first_call_takes_gain_change_branch():
    """cal.low_gain = -1 (uninit) → gain_change branch → BBP RXO 14 + 18
    get written + agc_gain_init feeds the gain_set path."""
    cal = phy.Mt76x2CalState(agc_gain_init=(0x20, 0x21))
    t = FakeTransport()
    phy.update_channel_gain(
        t, cal,
        band_2g=True,
        bw_40plus=False,
        has_ext_lna=False,
        rssi_thresh=C.MT76X2_RSSI_GAIN_THRESH_2G,
        low_rssi_thresh=C.MT76X2_LOW_RSSI_GAIN_THRESH_2G,
        avg_rssi_all=-75,
    )
    addrs = [a for a, _ in t.writes]
    assert C.MT_BBP_RXO_R14 in addrs
    assert C.MT_BBP_RXO_R18 in addrs
    assert C.MT_BBP_AGC_R35 in addrs
    assert C.MT_BBP_AGC_R37 in addrs
    # cal mutated.
    assert cal.low_gain >= 0
    assert cal.avg_rssi_all == -75


def test_update_channel_gain_second_call_with_stable_gain_takes_adjust_branch():
    """Second call with same low_gain → no BBP RXO/AGC retune, just
    `adjust_vga_gain` + (optional) `phy_set_gain_val`. With CCA in the
    middle range nothing should write."""
    cal = phy.Mt76x2CalState(agc_gain_init=(0x20, 0x21), low_gain=2)
    t = FakeTransport({C.MT_RX_STAT_1: 100})   # middle range → no change
    phy.update_channel_gain(
        t, cal,
        band_2g=True,
        bw_40plus=False,
        has_ext_lna=False,
        rssi_thresh=C.MT76X2_RSSI_GAIN_THRESH_2G,
        low_rssi_thresh=C.MT76X2_LOW_RSSI_GAIN_THRESH_2G,
        avg_rssi_all=-30,    # high RSSI → low_gain = 2 (unchanged)
    )
    addrs = [a for a, _ in t.writes]
    # Adjust branch with no CCA change → no BBP writes at all.
    assert C.MT_BBP_RXO_R14 not in addrs
    assert C.MT_BBP_RXO_R18 not in addrs


# ---------------------------------------------------------------------------
# MCU commands — payload bytes must match the kernel / wire exactly.
# ---------------------------------------------------------------------------

class FakeMcu:
    """Records send() calls; transport serves EEPROM reads."""

    def __init__(self, reads: dict[int, int] | None = None):
        self.transport = FakeTransport(reads)
        self.sends: list[tuple[int, bytes]] = []

    async def send(self, cmd, payload, wait_resp=False, resp_timeout_ms=0):
        self.sends.append((cmd, bytes(payload)))
        return True


async def test_mcu_load_cr_sends_8_bytes_with_cfg_dword():
    """`mcu_load_cr` matches the wire (capture-1 frame 2457): cr_mode=2,
    8-byte payload ending in cfg = BIT(31) | NIC_CONF nibbles."""
    # NIC_CONF_0=0xFF00, NIC_CONF_1=0x0000 → cfg low byte 0xFF, high 0x00.
    word = 0x0000FF00   # low16 = NIC_CONF_0, high16 = NIC_CONF_1
    mcu = FakeMcu({C.MT_VEND_TYPE_EEPROM | 0x034: word})
    ok = await phy.mcu_load_cr(mcu)
    assert ok
    assert len(mcu.sends) == 1
    cmd, payload = mcu.sends[0]
    assert cmd == phy.CMD_LOAD_CR
    assert len(payload) == 8, payload.hex()
    assert payload[0] == 2          # MT_RF_BBP_CR
    assert payload[1:4] == b"\x00\x00\x00"
    cfg = int.from_bytes(payload[4:8], "little")
    assert cfg == 0x800000FF, hex(cfg)


async def test_mcu_set_channel_double_sends_with_ext_chan_0_then_e0():
    """`mcu_set_channel` sends SWITCH_CHANNEL_OP twice: ext_chan 0x00 then
    0xe0 + bw_index (capture-1 frames 3005/3009)."""
    mcu = FakeMcu()
    ok = await phy.mcu_set_channel(mcu, channel=1, bw=0, bw_index=0,
                                   scan=False, chainmask=0x0202)
    assert ok
    assert len(mcu.sends) == 2
    for cmd, _ in mcu.sends:
        assert cmd == phy.CMD_SWITCH_CHANNEL_OP
    # ext_chan is byte index 6 in the 8-byte struct.
    assert mcu.sends[0][1][6] == 0x00
    assert mcu.sends[1][1][6] == 0xE0
    # idx/chainmask identical across both sends.
    assert mcu.sends[0][1][0] == 1 and mcu.sends[1][1][0] == 1


async def test_mcu_set_channel_ext_chan_includes_bw_index():
    """Second send's ext_chan = 0xe0 + bw_index."""
    mcu = FakeMcu()
    await phy.mcu_set_channel(mcu, channel=36, bw=1, bw_index=2,
                              scan=False, chainmask=0x0202)
    assert mcu.sends[1][1][6] == 0xE0 + 2
