"""rtl8821cu_dkms RX RSSI decode — the CCK old-AGC LNA-gain table selection (H3).

The old-AGC CCK RSSI uses a per-card LNA-gain table keyed on cck_agc_report_type: 1 (BTG, the
rfe-0x22 reference) -> the 16-entry table_1; 0 (WLG/WLA) -> the 8-entry table_0.
[SRC] phydm_cck_rssi_8821c phydm_hal_api8821c.c:42-60 / phydm_cck_lna_bit_num_chk phydm.c:178-185.
"""
from wifit3.chips.rtl8821cu_dkms import rx


def _cck_phystatus(lna_idx: int, vga_idx: int) -> bytes:
    """A CCK (page-0) Jaguar-2 phy-status: byte0 nibble 0, byte13 = vga[4:0] | lna_l<<5,
    byte14 bit7 = lna_h. decode_rssi reads bytes 13/14 for the old-AGC LNA/VGA."""
    b = bytearray(16)
    b[13] = (vga_idx & 0x1F) | ((lna_idx & 0x7) << 5)
    b[14] = ((lna_idx >> 3) & 0x1) << 7
    return bytes(b)


def test_cck_rssi_btg_uses_table_1():
    # report_type 1 (BTG): table_1[4] = -6, minus 2*vga(0).
    rssi = rx.decode_rssi(_cck_phystatus(lna_idx=4, vga_idx=0), cck_new_agc=False, cck_report_type=1)
    assert rssi == -6


def test_cck_rssi_wlg_uses_table_0():
    # report_type 0 (WLG/WLA): table_0[4] = -31, minus 2*vga(0).
    rssi = rx.decode_rssi(_cck_phystatus(lna_idx=4, vga_idx=0), cck_new_agc=False, cck_report_type=0)
    assert rssi == -31


def test_cck_rssi_vga_backoff_applies():
    # -6 (table_1[4]) - 2*3 = -12.
    rssi = rx.decode_rssi(_cck_phystatus(lna_idx=4, vga_idx=3), cck_new_agc=False, cck_report_type=1)
    assert rssi == -12


def test_cck_rssi_table_0_index_clamped_not_oob():
    # A 4-bit LNA index of 10 exceeds the 8-entry table_0; clamp to the last entry (-52), no crash.
    rssi = rx.decode_rssi(_cck_phystatus(lna_idx=10, vga_idx=0), cck_new_agc=False, cck_report_type=0)
    assert rssi == -52
