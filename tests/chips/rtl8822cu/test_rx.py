"""Hardware-free regression for the rtl8822cu JGR3 PHY-status RSSI decode.

Pins the two defects the vendor phydm parse removed: the invented min(0, ...) ceiling
that saturated strong signals to 0 dBm, and dropping OFDM phy-status types 2..5.
Expected dBm come from the vendor formula pwdb = (s8)byte - 110, floor -120, no upper
clamp (phydm_get_physts_0_jgr3 / phydm_get_physts_ofdm_cmn_jgr3, phydm_phystatus.c).
"""
from wifit3.chips.rtl8822cu.rx import _phy_rssi, iter_bulk_frames


def _cck(pwdb_a, gain_a=0):
    """A JGR3 type-0 (CCK) PHY-status report: type nibble 0, path-A pwdb at byte 1,
    gain_a (low 6 bits) at byte 2."""
    return bytes([0x00, pwdb_a & 0xFF, gain_a & 0x3F]) + b"\x00" * 29


def _cck_report(pwdb_a, gain_a, pwdb_b, gain_b):
    """A full 32-byte JGR3 type-0 report with both RX paths: path B pwdb at byte 16,
    gain_b at byte 19 (phy_sts_rpt_jgr3_type0)."""
    b = bytearray(32)
    b[1], b[2] = pwdb_a & 0xFF, gain_a & 0x3F
    b[16], b[19] = pwdb_b & 0xFF, gain_b & 0x3F
    return bytes(b)


def _ofdm(phy_type, pwdb0, pwdb1):
    """A JGR3 OFDM report of the given type (1..5): pwdb[0..1] at bytes 1..2."""
    return bytes([phy_type & 0x0F, pwdb0 & 0xFF, pwdb1 & 0xFF]) + b"\x00" * 29


def _rssi(phy_status, cck_gi_l_bnd=None, cck_gi_u_bnd=None):
    return _phy_rssi(phy_status, 0, None, cck_gi_l_bnd, cck_gi_u_bnd)


# --- CCK type 0 -------------------------------------------------------------

def test_cck_decodes_signed_byte_minus_110():
    assert _rssi(_cck(43)) == -67          # 43 - 110
    assert _rssi(_cck(95)) == -15          # strong but valid


def test_cck_strong_signal_not_saturated_to_zero():
    # pre-fix min(0, 115 - 110) collapsed any byte >= 110 to exactly 0 dBm.
    assert _rssi(_cck(115)) == 5


def test_cck_high_byte_read_as_signed_not_plus_134():
    # pre-s8 244 - 110 = +134 (impossible); (s8)244 = -12 -> -122 -> floor.
    assert _rssi(_cck(244)) == -120


def test_cck_floor_at_minus_120():
    assert _rssi(_cck(200)) == -120        # (s8)200 = -56 -> -166 -> floor
    assert _rssi(_cck(10)) == -100         # 10 - 110, above the floor


# --- OFDM types 1..5 (one shared pwdb layout) -------------------------------

def test_ofdm_type1_takes_strongest_path():
    assert _rssi(_ofdm(1, 34, 32)) == -76  # max(34, 32) - 110
    assert _rssi(_ofdm(1, 20, 34)) == -76  # path B stronger


def test_ofdm_types_2_through_5_now_decode():
    # pre-fix only pages 0 and 1 were handled; 2..5 returned None (-> -100 upstream).
    for phy_type in (2, 3, 4, 5):
        assert _rssi(_ofdm(phy_type, 40, 38)) == -70


def test_type_6_and_above_unhandled():
    assert _rssi(_ofdm(6, 40, 38)) is None


def test_short_buffer_returns_none():
    assert _rssi(b"\x00\x01") is None


# --- through the shipped public path ----------------------------------------

def _desc(pkt_len, drvinfo_sz, shift_sz=0, physt=1):
    dw0 = ((pkt_len & 0x3FFF) | ((drvinfo_sz // 8) << 16)
           | (shift_sz << 24) | (physt << 26))
    return dw0.to_bytes(4, "little") + b"\x00" * 20   # dw0 + dw1..dw5 = 24 bytes


def _pkt(payload_with_fcs, phy_status, *, drvinfo_sz=32, shift_sz=0):
    drvinfo = (phy_status + b"\x00" * drvinfo_sz)[:drvinfo_sz]
    body = (_desc(len(payload_with_fcs), drvinfo_sz, shift_sz)
            + drvinfo + b"\x00" * shift_sz + payload_with_fcs)
    return body + b"\x00" * ((-len(body)) % 8)


def test_rssi_flows_through_iter_bulk_frames():
    buf = _pkt(b"WXYZ" + b"\xde\xad\xbe\xef", _ofdm(1, 34, 20))
    (stat, mpdu, rssi), = iter_bulk_frames(buf)
    assert mpdu == b"WXYZ"                 # FCS stripped
    assert rssi == -76                     # 34 - 110


# --- CCK gain correction (phydm_cck_gi_bound_8822c bounds) -------------------
# The RTL8822C device in captures_rtl88x2cu reads cck_gi_l_bnd=16, cck_gi_u_bnd=63.

def test_cck_gain_correction_rescues_weak_beacon_from_floor():
    # Weak CCK beacon: pwdb_a=244 (s8 -12), gain_a=0 < l_bnd=16.
    # rx_power = -12 + (16-0)<<1 = 20 -> 20 - 110 = -90.
    assert _rssi(_cck(244, gain_a=0), 16, 63) == -90
    # Without the bounds the same frame floors, which is the bug being fixed.
    assert _rssi(_cck(244, gain_a=0)) == -120


def test_cck_gain_correction_upper_bound_cut():
    # gain above the upper bound subtracts: pwdb_a=95, gain_a=40, u_bnd=30.
    # rx_power = 95 - (40-30)<<1 = 75 -> 75 - 110 = -35.
    assert _rssi(_cck(95, gain_a=40), 16, 30) == -35


def test_cck_no_correction_when_gain_in_band():
    # l_bnd(16) <= gain(20) <= u_bnd(63): no adjustment, 95 - 110 = -15.
    assert _rssi(_cck(95, gain_a=20), 16, 63) == -15


def test_cck_takes_stronger_of_two_paths_through_iter_bulk_frames():
    # path A weak (244 -> corrected 20 -> -90), path B strong (90 in band -> -20).
    buf = _pkt(b"WXYZ" + b"\xde\xad\xbe\xef", _cck_report(244, 0, 90, 20))
    (_stat, mpdu, rssi), = iter_bulk_frames(buf, cck_gi_l_bnd=16, cck_gi_u_bnd=63)
    assert mpdu == b"WXYZ"
    assert rssi == -20                     # path B wins


def test_cck_path_b_ignored_when_report_too_short():
    # drv_info_sz=16 (< 20): path B bytes are outside the report, so path A only.
    buf = _pkt(b"WXYZ" + b"\x00\x00\x00\x00", _cck_report(50, 20, 200, 0), drvinfo_sz=16)
    (_stat, _mpdu, rssi), = iter_bulk_frames(buf, cck_gi_l_bnd=16, cck_gi_u_bnd=63)
    assert rssi == -60                     # 50 in band -> 50 - 110, path B (200) ignored
