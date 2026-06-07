"""Hardware-free regression for the RTL8188EUS (DKMS) thermal TX-power tracking.

SAFETY-CRITICAL: locks the swing-index math + the exact BB-swing register values, so a
bad delta table or an off-by-one can't silently change TX power. The cold-boot case
(efuse base 29, init swing OFDM 28 / CCK 20, thermal read 25 -> the rate-selected CCK-A
delta table -> absolute -3) must write OFDM idx 25 (0xc80=0x300000c0) + CCK idx 17
(0xa22=0x17 ...), matching the wire.
"""
from wifit3.chips.rtl8188eus_dkms import powertrack

_RF = "wifit3.chips.rtl8188eus_dkms.powertrack.rf"


class RegTx:
    def __init__(self, regs=None):
        self.regs = dict(regs or {})
        self.w = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w.append((a, v))

    def write8(self, a, v):
        v &= 0xFF
        self.regs[a] = v
        self.w.append((a, v))


def test_swing_index_lookup():
    # get_swing_index matches 0xc80[31:22]; get_cck_swing_index matches byte 0xa22.
    assert powertrack.get_swing_index(0x390000E4) == 28
    assert powertrack.get_cck_swing_index(0x1B) == 20


def test_apply_mix_mode_writes_base_swing():
    st = powertrack.PowerTrackState(eeprom_thermal=29, default_ofdm_index=28,
                                    default_cck_index=20)
    t = RegTx({0x0C4C: 0x001C0324})
    powertrack._apply_mix_mode(t, st, -3)            # final OFDM 25, CCK 17
    assert (0x0C80, 0x300000C0) in t.w               # OFDM_SWING_TABLE[25]
    assert (0x0A22, 0x17) in t.w                     # CCK row 17 [0]
    assert (0x0A29, 0x02) in t.w                     # CCK row 17 [7]


def test_callback_cold_boot_drops_three_indices(mocker):
    # thermal read 25 < efuse base 29 -> ad=4 -> -CCK_A_N[4]=-3 -> idx 25/17.
    mocker.patch(f"{_RF}.phy_query_rf_reg", return_value=25)
    st = powertrack.PowerTrackState(eeprom_thermal=29, default_ofdm_index=28,
                                    default_cck_index=20)
    t = RegTx({0x0C4C: 0x001C0324})
    powertrack._callback(t, st)
    assert st.delta_power_index == -3
    assert (0x0C80, 0x300000C0) in t.w
    assert (0x0A22, 0x17) in t.w


def test_callback_thermal_at_base_no_apply(mocker):
    # thermal == efuse base -> delta 0 -> power_index_offset 0 -> no swing write.
    mocker.patch(f"{_RF}.phy_query_rf_reg", return_value=29)
    st = powertrack.PowerTrackState(eeprom_thermal=29, default_ofdm_index=28,
                                    default_cck_index=20)
    t = RegTx({0x0C4C: 0x001C0324})
    powertrack._callback(t, st)
    assert not any(a == 0x0C80 for a, _ in t.w)      # no OFDM swing write
    assert not any(a == 0x0A22 for a, _ in t.w)      # no CCK swing write


def test_thermal_tick_arms_then_runs(mocker):
    # tm_trigger toggles: an un-armed tick arms the meter (RF write), no swing change.
    rfwrite = mocker.patch(f"{_RF}.set_rf_reg")
    st = powertrack.PowerTrackState(eeprom_thermal=29, default_ofdm_index=28,
                                    default_cck_index=20, tm_trigger=0)
    powertrack.thermal_tick(RegTx(), st)
    rfwrite.assert_called_once()
    assert st.tm_trigger == 1
