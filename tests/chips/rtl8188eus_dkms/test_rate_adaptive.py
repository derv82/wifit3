"""Hardware-free regression for the RTL8188E driver RA TX_REPORT2 handler and its REG_TX_RPT_TIME cmds."""
from wifit3.chips.rtl8188eus_dkms import rate_adaptive
from wifit3.chips.rtl8188eus_dkms.phy_cond import ODM_CUT_A, ODM_CUT_I

_VALID_MACID_1 = 1 << 1


def _report(mac1_item: str) -> bytes:
    """Two 8 B items (macid 0, macid 1); only macid 1 is valid."""
    return bytes(8) + bytes.fromhex(mac1_item)


def test_first_report_programs_default_rpt_time_once():
    cmds = []
    ra = rate_adaptive.RateAdaptive(ODM_CUT_A, cmds.append)
    ra.odm_ra_tx_rpt2_handle_8188e(_report("0000000000000000"), 16, _VALID_MACID_1, 0)
    ra.odm_ra_tx_rpt2_handle_8188e(_report("0000000000000000"), 16, _VALID_MACID_1, 0)
    assert cmds == [0x927C]


def test_rate_down_shortens_rpt_time():
    cmds = []
    ra = rate_adaptive.RateAdaptive(ODM_CUT_A, cmds.append)
    ra.odm_ra_tx_rpt2_handle_8188e(_report("0000000000000000"), 16, _VALID_MACID_1, 0)
    ra.odm_ra_tx_rpt2_handle_8188e(_report("4100140201020000"), 16, _VALID_MACID_1, 0)   # rate down
    ra.odm_ra_tx_rpt2_handle_8188e(_report("0600010000000700"), 16, _VALID_MACID_1, 0)   # ra_drop_after_down
    ra.odm_ra_tx_rpt2_handle_8188e(_report("3500040207022e00"), 16, _VALID_MACID_1, 0)   # DROP > necessary
    assert cmds == [0x927C, 0x7A12, 0x61A8]
    assert ra.ra_info[1].decision_rate == 0x11


def test_invalid_macid_is_not_decided():
    cmds = []
    ra = rate_adaptive.RateAdaptive(ODM_CUT_A, cmds.append)
    ra.odm_ra_tx_rpt2_handle_8188e(_report("3500040207022e00"), 16, 0, 0)
    assert cmds == [0x927C] and ra.ra_info[1].rpt_time == 0x927C


def test_i_cut_selects_the_s_penalty_tables():
    ra = rate_adaptive.RateAdaptive(ODM_CUT_I, lambda value: None)
    assert ra.retry_penalty_idx is rate_adaptive.RETRY_PENALTY_IDX_S
    assert ra.retry_penalty_up_idx is rate_adaptive.RETRY_PENALTY_UP_IDX_S
