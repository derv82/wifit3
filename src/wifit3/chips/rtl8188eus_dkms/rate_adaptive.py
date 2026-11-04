"""RTL8188E driver rate adaptation: the TX_REPORT2 handler and the REG_TX_RPT_TIME timing it drives.

Port of [SRC] hal/phydm/rtl8188e/hal8188erateadaptive.c built for ODM_CE with RATE_ADAPTIVE_SUPPORT
and POWER_TRAINING_ACTIVE (include/hal_ic_cfg.h:51-52); the ODM_AP / AP_USB_SDIO branches are not
compiled. Monitor mode registers no station (odm_ra_update_rate_info_8188e and odm_ra_set_rssi_8188e
run only for a linked one), so every entry keeps its odm_ra_info_init values for rate_id, rate_mask,
ra_use_rate, highest_rate, lowest_rate, pt_mode_ss and rssi_sta_ra, and odm_arfb_refresh_8188e is
unported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .phy_cond import ODM_CUT_I

ODM_RATEMCS7 = 0x13                     # phydm_pre_define.h:225
ODM_ASSOCIATE_ENTRY_NUM = 32            # MACID_NUM_SW_LIMIT (drv_conf.h:350) via phydm_pre_define.h:79-81
TX_RPT2_ITEM_SIZE = 8                   # hal8188erateadaptive.h:22

RETRY_PENALTY = (                       # hal8188erateadaptive.c:23 (non-AP rows)
    (5, 4, 3, 2, 0, 3), (6, 5, 4, 3, 0, 4), (6, 5, 4, 2, 0, 4), (8, 7, 6, 4, 0, 6),
    (10, 9, 8, 6, 0, 8), (10, 9, 8, 4, 0, 8), (10, 9, 8, 2, 0, 8), (10, 9, 8, 0, 0, 8),
    (18, 17, 16, 8, 0, 16), (26, 25, 24, 16, 0, 24), (34, 33, 32, 24, 0, 32),
    (34, 31, 28, 20, 0, 32), (34, 31, 27, 18, 0, 32), (34, 31, 26, 16, 0, 32),
    (34, 30, 22, 16, 0, 32), (34, 30, 24, 16, 0, 32), (49, 46, 40, 16, 0, 48),
    (49, 45, 32, 0, 0, 48), (49, 45, 22, 18, 0, 48), (49, 40, 24, 16, 0, 48),
    (49, 32, 18, 12, 0, 48), (49, 22, 18, 14, 0, 48), (49, 16, 16, 0, 0, 48),
)
PT_PENALTY = (34, 31, 30, 24, 0, 32)    # hal8188erateadaptive.c:72

RETRY_PENALTY_IDX = (                   # hal8188erateadaptive.c:133
    (4, 4, 4, 5, 4, 4, 5, 7, 7, 7, 8, 0x0a,
     4, 4, 4, 4, 6, 0x0a, 0x0b, 0x0d,
     5, 5, 7, 7, 8, 0x0b, 0x0d, 0x0f),
    (0x0a, 0x0a, 0x0b, 0x0c, 0x0a, 0x0a, 0x0b, 0x0c, 0x0d, 0x10, 0x13, 0x13,
     0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x11, 0x13, 0x13,
     9, 9, 9, 9, 0x0c, 0x0e, 0x11, 0x13),
)
RETRY_PENALTY_UP_IDX = (                # hal8188erateadaptive.c:145
    0x0c, 0x0d, 0x0d, 0x0f, 0x0d, 0x0e, 0x0f, 0x0f, 0x10, 0x12, 0x13, 0x14,
    0x0f, 0x10, 0x10, 0x12, 0x12, 0x13, 0x14, 0x15,
    0x11, 0x11, 0x12, 0x13, 0x13, 0x13, 0x14, 0x15,
)
RETRY_PENALTY_IDX_S = (                 # hal8188erateadaptive.c:954 (I-cut, odm_ra_info_init_all)
    (4, 4, 4, 5,
     4, 4, 5, 7, 7, 7, 8, 0x0a,
     4, 4, 4, 4, 6, 0x0a, 0x0b, 0x0d,
     5, 5, 7, 7, 8, 0x0b, 0x0d, 0x0f),
    (0x0a, 0x0a, 0x0b, 0x0c,
     0x0a, 0x0a, 0x0b, 0x0c, 0x0d, 0x10, 0x13, 0x13,
     0x06, 0x07, 0x08, 0x0d, 0x0e, 0x11, 0x11, 0x11,
     9, 9, 9, 9, 0x0c, 0x0e, 0x11, 0x13),
)
RETRY_PENALTY_UP_IDX_S = (              # hal8188erateadaptive.c:968 (I-cut)
    0x0c, 0x0d, 0x0d, 0x0f,
    0x0d, 0x0e, 0x0f, 0x0f, 0x10, 0x12, 0x13, 0x14,
    0x0b, 0x0b, 0x11, 0x11, 0x12, 0x12, 0x12, 0x12,
    0x11, 0x11, 0x12, 0x13, 0x13, 0x13, 0x14, 0x15,
)
RSSI_THRESHOLD = (                      # hal8188erateadaptive.c:150
    0, 0, 0, 0,
    0, 0, 0, 0, 0, 0x24, 0x26, 0x2a,
    0x18, 0x1a, 0x1d, 0x1f, 0x21, 0x27, 0x29, 0x2a,
    0, 0, 0, 0x1f, 0x23, 0x28, 0x2a, 0x2c,
)
N_THRESHOLD_HIGH = (                    # hal8188erateadaptive.c:170
    4, 4, 8, 16,
    24, 36, 48, 72, 96, 144, 192, 216,
    60, 80, 100, 160, 240, 400, 600, 800,
    300, 320, 480, 720, 1000, 1200, 1600, 2000,
)
N_THRESHOLD_LOW = (                     # hal8188erateadaptive.c:175
    2, 2, 4, 8,
    12, 18, 24, 36, 48, 72, 96, 108,
    30, 40, 50, 80, 120, 200, 300, 400,
    150, 160, 240, 360, 500, 600, 800, 1000,
)
DROPING_NECESSARY = (                   # hal8188erateadaptive.c:193
    1, 1, 1, 1,
    1, 2, 3, 4, 5, 6, 7, 8,
    1, 2, 3, 4, 5, 6, 7, 8,
    5, 6, 7, 8, 9, 10, 11, 12,
)
pending_for_rate_up_fail = (2, 10, 24, 40, 60)                                 # :218
dynamic_tx_rpt_timing = (0x186a, 0x30d4, 0x493e, 0x61a8, 0x7a12, 0x927c)       # :219, 200-1200 ms


@dataclass
class OdmRaInfo:
    """struct _odm_ra_info_ (phydm_rainfo.h:117) as odm_ra_info_init (hal8188erateadaptive.c:878) sets it."""
    decision_rate: int = ODM_RATEMCS7
    pre_rate: int = ODM_RATEMCS7
    highest_rate: int = ODM_RATEMCS7
    lowest_rate: int = 0
    rssi_sta_ra: int = 0
    pre_rssi_sta_ra: int = 0
    sgi_enable: int = 0
    ra_use_rate: int = 0xFFFFFFFF
    nsc_down: int = (N_THRESHOLD_HIGH[0x13] + N_THRESHOLD_LOW[0x13]) // 2
    nsc_up: int = (N_THRESHOLD_HIGH[0x13] + N_THRESHOLD_LOW[0x13]) // 2
    rate_sgi: int = 0
    active: int = 1
    rpt_time: int = 0x927C
    DROP: int = 0
    RTY: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    TOTAL: int = 0
    ra_waiting_counter: int = 0
    ra_pending_counter: int = 0
    ra_drop_after_down: int = 0
    pt_active: int = 1
    pt_try_state: int = 0
    pt_stage: int = 5
    pt_smooth_factor: int = 192
    pt_stop_count: int = 0
    pt_pre_rate: int = 0
    pt_pre_rssi: int = 0
    pt_mode_ss: int = 0
    ra_stage: int = 0


class RateAdaptive:
    """The dm-level RA state (dm->ra_info[], dm->currmin_rpt_time); rtw_rpt_timer_cfg_cmd queues the
    REG_TX_RPT_TIME write to the command thread (rtw_cmd.c:3025)."""

    def __init__(self, cut_version: int, rtw_rpt_timer_cfg_cmd: Callable[[int], None]) -> None:
        # odm_ra_info_init_all (hal8188erateadaptive.c:934)
        self.currmin_rpt_time = 0
        self.ra_info = [OdmRaInfo() for _ in range(ODM_ASSOCIATE_ENTRY_NUM)]
        if cut_version == ODM_CUT_I:
            self.retry_penalty_idx = RETRY_PENALTY_IDX_S
            self.retry_penalty_up_idx = RETRY_PENALTY_UP_IDX_S
        else:
            self.retry_penalty_idx = RETRY_PENALTY_IDX
            self.retry_penalty_up_idx = RETRY_PENALTY_UP_IDX
        self.dynamic_tx_rpt_timing_counter = 0     # function-static in odm_rate_decision_8188e
        self.rtw_rpt_timer_cfg_cmd = rtw_rpt_timer_cfg_cmd

    def odm_ra_tx_rpt2_handle_8188e(self, tx_rpt_buf: bytes, tx_rpt_len: int,
                                    mac_id_valid_entry0: int, mac_id_valid_entry1: int) -> None:
        """hal8188erateadaptive.c:1213. Items past the ra_info table are ignored (the C indexes past it)."""
        min_rpt_time = 0x927C
        item_num = tx_rpt_len >> 3
        mac_id = 0
        while True:
            valid = 0
            if mac_id < 32:
                valid = (1 << mac_id) & mac_id_valid_entry0
            p_ra_info = self.ra_info[mac_id]
            if valid:
                p_buffer = tx_rpt_buf[mac_id * TX_RPT2_ITEM_SIZE:(mac_id + 1) * TX_RPT2_ITEM_SIZE]
                p_ra_info.RTY[0] = int.from_bytes(p_buffer[0:2], "little")  # GET_TX_REPORT_TYPE1_RERTY_0
                p_ra_info.RTY[1] = p_buffer[2]
                p_ra_info.RTY[2] = p_buffer[3]
                p_ra_info.RTY[3] = p_buffer[4]
                p_ra_info.RTY[4] = p_buffer[5]
                p_ra_info.DROP = p_buffer[6]
                p_ra_info.TOTAL = sum(p_ra_info.RTY) + p_ra_info.DROP
                if p_ra_info.TOTAL != 0:
                    # POWER_TRAINING_ACTIVE == 1
                    if p_ra_info.pt_active:
                        if p_ra_info.ra_stage < 5:
                            self.odm_rate_decision_8188e(p_ra_info)
                        elif p_ra_info.ra_stage == 5:
                            odm_pt_try_state_8188e(p_ra_info)
                        else:
                            odm_pt_decision_8188e(p_ra_info)
                        if p_ra_info.ra_stage <= 5:
                            p_ra_info.ra_stage += 1
                        else:
                            p_ra_info.ra_stage = 0
                    else:
                        self.odm_rate_decision_8188e(p_ra_info)
            if min_rpt_time > p_ra_info.rpt_time:
                min_rpt_time = p_ra_info.rpt_time
            mac_id += 1
            if mac_id >= item_num or mac_id >= ODM_ASSOCIATE_ENTRY_NUM:
                break
        self.odm_ra_tx_rpt_timer_setting(min_rpt_time)

    def odm_ra_tx_rpt_timer_setting(self, min_rpt_time: int) -> None:
        """hal8188erateadaptive.c:840."""
        if self.currmin_rpt_time != min_rpt_time:
            self.rtw_rpt_timer_cfg_cmd(min_rpt_time)
            self.currmin_rpt_time = min_rpt_time

    def odm_rate_decision_8188e(self, p_ra_info: OdmRaInfo) -> None:
        """hal8188erateadaptive.c:501. The rate-update phydm_c2h_ra_report_handler call is unported: it
        only updates a linked station's cmn_sta_info."""
        if not (p_ra_info.active and p_ra_info.TOTAL > 0):
            return
        if p_ra_info.ra_drop_after_down:
            p_ra_info.ra_drop_after_down -= 1
            odm_reset_ra_counter_8188e(p_ra_info)
            return
        if (p_ra_info.rssi_sta_ra < p_ra_info.pre_rssi_sta_ra - 3
                or p_ra_info.rssi_sta_ra > p_ra_info.pre_rssi_sta_ra + 3):
            p_ra_info.pre_rssi_sta_ra = p_ra_info.rssi_sta_ra
            p_ra_info.ra_waiting_counter = 0
            p_ra_info.ra_pending_counter = 0

        if p_ra_info.pre_rate > p_ra_info.highest_rate:
            rate_id = p_ra_info.highest_rate
        else:
            rate_id = p_ra_info.pre_rate
        if p_ra_info.rssi_sta_ra > RSSI_THRESHOLD[rate_id]:
            rty_pt_id = 0
        else:
            rty_pt_id = 1
        penalty_id1 = self.retry_penalty_idx[rty_pt_id][rate_id]
        for j in range(5):
            p_ra_info.nsc_down += p_ra_info.RTY[j] * RETRY_PENALTY[penalty_id1][j]
        if p_ra_info.nsc_down > p_ra_info.TOTAL * RETRY_PENALTY[penalty_id1][5]:
            p_ra_info.nsc_down -= p_ra_info.TOTAL * RETRY_PENALTY[penalty_id1][5]
        else:
            p_ra_info.nsc_down = 0

        penalty_id2 = self.retry_penalty_up_idx[rate_id]
        for j in range(5):
            p_ra_info.nsc_up += p_ra_info.RTY[j] * RETRY_PENALTY[penalty_id2][j]
        if p_ra_info.nsc_up > p_ra_info.TOTAL * RETRY_PENALTY[penalty_id2][5]:
            p_ra_info.nsc_up -= p_ra_info.TOTAL * RETRY_PENALTY[penalty_id2][5]
        else:
            p_ra_info.nsc_up = 0

        if (p_ra_info.nsc_down < N_THRESHOLD_LOW[rate_id]
                or p_ra_info.DROP > DROPING_NECESSARY[rate_id]):
            odm_rate_down_8188e(p_ra_info)
        elif p_ra_info.nsc_up > N_THRESHOLD_HIGH[rate_id]:
            odm_rate_up_8188e(p_ra_info)

        if p_ra_info.decision_rate == p_ra_info.pre_rate:
            self.dynamic_tx_rpt_timing_counter = (self.dynamic_tx_rpt_timing_counter + 1) & 0xFF
        else:
            self.dynamic_tx_rpt_timing_counter = 0
        if self.dynamic_tx_rpt_timing_counter >= 4:
            odm_set_tx_rpt_timing_8188e(p_ra_info, 1)
            self.dynamic_tx_rpt_timing_counter = 0
        p_ra_info.pre_rate = p_ra_info.decision_rate
        odm_reset_ra_counter_8188e(p_ra_info)


def odm_set_tx_rpt_timing_8188e(p_ra_info: OdmRaInfo, extend: int) -> None:
    """hal8188erateadaptive.c:291. extend 0 = default, 1 = longer, 2 = shorter."""
    idx = 0
    while idx < 5:
        if dynamic_tx_rpt_timing[idx] == p_ra_info.rpt_time:
            break
        idx += 1
    if extend == 0:
        idx = 0
    elif extend == 1:
        idx += 1
        if idx > 5:
            idx = 5
    elif extend == 2:
        if idx != 0:
            idx -= 1
    p_ra_info.rpt_time = dynamic_tx_rpt_timing[idx]


def odm_rate_down_8188e(p_ra_info: OdmRaInfo) -> None:
    """hal8188erateadaptive.c:319."""
    rate_id = p_ra_info.pre_rate
    lowest_rate = p_ra_info.lowest_rate
    highest_rate = p_ra_info.highest_rate
    if rate_id > highest_rate:
        rate_id = highest_rate
    elif p_ra_info.rate_sgi:
        p_ra_info.rate_sgi = 0
    elif rate_id > lowest_rate:
        if rate_id > 0:
            for i in range(rate_id - 1, lowest_rate - 1, -1):
                if p_ra_info.ra_use_rate & (1 << i):
                    rate_id = i
                    break
    elif rate_id <= lowest_rate:
        rate_id = lowest_rate

    if p_ra_info.ra_waiting_counter == 1:
        p_ra_info.ra_waiting_counter += 1
        p_ra_info.ra_pending_counter += 1
    elif p_ra_info.ra_waiting_counter != 0:
        p_ra_info.ra_waiting_counter = 0
        p_ra_info.ra_pending_counter = 0
    if p_ra_info.ra_pending_counter >= 4:
        p_ra_info.ra_pending_counter = 4
    p_ra_info.ra_drop_after_down = 1
    p_ra_info.decision_rate = rate_id
    odm_set_tx_rpt_timing_8188e(p_ra_info, 2)


def odm_rate_up_8188e(p_ra_info: OdmRaInfo) -> None:
    """hal8188erateadaptive.c:412."""
    rate_id = p_ra_info.pre_rate
    highest_rate = p_ra_info.highest_rate
    skip_to_finish = False
    if p_ra_info.ra_waiting_counter == 1:
        p_ra_info.ra_waiting_counter = 0
        p_ra_info.ra_pending_counter = 0
    elif p_ra_info.ra_waiting_counter > 1:
        p_ra_info.pre_rssi_sta_ra = p_ra_info.rssi_sta_ra
        skip_to_finish = True
    if not skip_to_finish:
        odm_set_tx_rpt_timing_8188e(p_ra_info, 0)
        if rate_id < highest_rate:
            for i in range(rate_id + 1, highest_rate + 1):
                if p_ra_info.ra_use_rate & (1 << i):
                    rate_id = i
                    break
        elif rate_id == highest_rate:
            if p_ra_info.sgi_enable and p_ra_info.rate_sgi != 1:
                p_ra_info.rate_sgi = 1
            elif p_ra_info.sgi_enable != 1:
                p_ra_info.rate_sgi = 0
        else:
            rate_id = highest_rate

    # rate_up_finish
    if p_ra_info.ra_waiting_counter == 4 + pending_for_rate_up_fail[p_ra_info.ra_pending_counter]:
        p_ra_info.ra_waiting_counter = 0
    else:
        p_ra_info.ra_waiting_counter = (p_ra_info.ra_waiting_counter + 1) & 0xFF
    p_ra_info.decision_rate = rate_id


def odm_reset_ra_counter_8188e(p_ra_info: OdmRaInfo) -> None:
    """hal8188erateadaptive.c:492."""
    rate_id = p_ra_info.decision_rate
    p_ra_info.nsc_up = (N_THRESHOLD_HIGH[rate_id] + N_THRESHOLD_LOW[rate_id]) >> 1
    p_ra_info.nsc_down = (N_THRESHOLD_HIGH[rate_id] + N_THRESHOLD_LOW[rate_id]) >> 1


def odm_pt_try_state_8188e(p_ra_info: OdmRaInfo) -> None:
    """hal8188erateadaptive.c:734. dm->is_disable_power_training stays false: its only writer,
    phydm_update_power_training_state, is built only with PHYDM_POWER_TRAINING_SUPPORT (8822B)."""
    p_ra_info.pt_try_state = 0
    if p_ra_info.pt_mode_ss == 3:
        if p_ra_info.decision_rate >= 0x19:
            p_ra_info.pt_try_state = 1
    elif p_ra_info.pt_mode_ss == 2:
        if p_ra_info.decision_rate >= 0x11:
            p_ra_info.pt_try_state = 1
    elif p_ra_info.pt_mode_ss == 1:
        if p_ra_info.decision_rate >= 0x0a:
            p_ra_info.pt_try_state = 1
    elif p_ra_info.pt_mode_ss == 0:
        if p_ra_info.decision_rate >= 0x03:
            p_ra_info.pt_try_state = 1

    if p_ra_info.rssi_sta_ra < 48:
        p_ra_info.pt_stage = 0
    elif p_ra_info.pt_try_state == 1:
        if (p_ra_info.pt_stop_count >= 10
                or p_ra_info.pt_pre_rssi > p_ra_info.rssi_sta_ra + 5
                or p_ra_info.pt_pre_rssi < p_ra_info.rssi_sta_ra - 5
                or p_ra_info.decision_rate != p_ra_info.pt_pre_rate):
            if p_ra_info.pt_stage == 0:
                p_ra_info.pt_stage = 1
            elif p_ra_info.pt_stage == 1:
                p_ra_info.pt_stage = 3
            else:
                p_ra_info.pt_stage = 5
            p_ra_info.pt_pre_rssi = p_ra_info.rssi_sta_ra
            p_ra_info.pt_stop_count = 0
        else:
            p_ra_info.ra_stage = 0
            p_ra_info.pt_stop_count += 1
    else:
        p_ra_info.pt_stage = 0
        p_ra_info.ra_stage = 0
    p_ra_info.pt_pre_rate = p_ra_info.decision_rate


def odm_pt_decision_8188e(p_ra_info: OdmRaInfo) -> None:
    """hal8188erateadaptive.c:798."""
    numsc = 0
    num_total = p_ra_info.TOTAL * PT_PENALTY[5]
    j = 0
    while j <= 4:
        numsc += p_ra_info.RTY[j] * PT_PENALTY[j]
        if numsc > num_total:
            break
        j += 1
    j = j >> 1
    temp_stage = (p_ra_info.pt_stage + 1) >> 1
    if temp_stage > j:
        stage_id = temp_stage - j
    else:
        stage_id = 0
    p_ra_info.pt_smooth_factor = ((p_ra_info.pt_smooth_factor >> 1) + (p_ra_info.pt_smooth_factor >> 2)
                                  + stage_id * 16 + 2)
    if p_ra_info.pt_smooth_factor > 192:
        p_ra_info.pt_smooth_factor = 192
    stage_id = p_ra_info.pt_smooth_factor >> 6
    temp_stage = stage_id * 2
    if temp_stage != 0:
        temp_stage -= 1
    if p_ra_info.DROP > 3:
        temp_stage = 0
    p_ra_info.pt_stage = temp_stage
