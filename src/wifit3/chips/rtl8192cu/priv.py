"""The slices of struct rtl_priv / struct rtl_usb (wifi.h, usb.h) the rtl8192cu port reads and writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import constants as C


class RegisterIo(Protocol):
    """rtlpriv->io: the synchronous register accessors usb.c installs."""
    def read8(self, addr: int) -> int: ...
    def read16(self, addr: int) -> int: ...
    def read32(self, addr: int) -> int: ...
    def write8(self, addr: int, value: int) -> None: ...
    def write16(self, addr: int, value: int) -> None: ...
    def write32(self, addr: int, value: int) -> None: ...
    def write_chunk(self, addr: int, data: bytes) -> None: ...


def _rf_path_rows(width: int) -> list[list[int]]:
    return [[0] * width for _ in range(C.MAX_RF_PATH)]


@dataclass
class RtlHal:
    version: int = C.VERSION_UNKNOWN
    hw_type: int = C.HARDWARE_TYPE_RTL8192E
    interface: int = C.INTF_USB
    state: int = C._HAL_STATE_STOP
    oem_id: int = C.RT_CID_DEFAULT
    board_type: int = 0
    fw_ready: bool = False
    fw_name: str = ""
    pfirmware: bytes | None = None
    fwsize: int = 0
    fw_version: int = 0
    fw_subversion: int = 0
    last_hmeboxnum: int = 0


@dataclass
class BbRegDef:
    """struct bb_reg_def (wifi.h:388)."""
    rfintfs: int = 0
    rfintfi: int = 0
    rfintfo: int = 0
    rfintfe: int = 0
    rf3wire_offset: int = 0
    rflssi_select: int = 0
    rftxgain_stage: int = 0
    rfhssi_para1: int = 0
    rfhssi_para2: int = 0
    rfsw_ctrl: int = 0
    rfagc_control1: int = 0
    rfagc_control2: int = 0
    rfrxiq_imbal: int = 0
    rfrx_afe: int = 0
    rftxiq_imbal: int = 0
    rftx_afe: int = 0
    rf_rb: int = 0
    rf_rbpi: int = 0


@dataclass
class RtlPhy:
    phyreg_def: list[BbRegDef] = field(default_factory=lambda: [BbRegDef() for _ in range(4)])
    rf_mode: int = C.RF_OP_BY_SW_3WIRE
    rf_type: int = C.RF_1T1R
    num_total_rfpath: int = 0
    current_channel: int = 0
    current_chan_bw: int = C.HT_CHANNEL_WIDTH_20
    set_bwmode_inprogress: bool = False
    sw_chnl_inprogress: bool = False
    sw_chnl_stage: int = 0
    sw_chnl_step: int = 0
    cur_cck_txpwridx: int = 0
    cur_ofdm24g_txpwridx: int = 0
    cck_high_power: bool = False
    pwrgroup_cnt: int = 0
    mcs_txpwrlevel_origoffset: list[list[int]] = field(
        default_factory=lambda: [[0] * 16 for _ in range(C.MAX_PG_GROUP)])
    mcs_offset: list[list[int]] = field(default_factory=lambda: [[0] * 16 for _ in range(C.MAX_PG_GROUP)])
    hwparam_tables: list[tuple[int, ...]] = field(default_factory=lambda: [() for _ in range(C.MAX_TAB)])
    rfreg_chnlval: list[int] = field(default_factory=lambda: [0, 0])
    iqk_initialized: bool = False
    rfpi_enable: int = 0
    reg_c04: int = 0
    reg_c08: int = 0
    reg_874: int = 0
    reg_e94: int = 0
    reg_e9c: int = 0
    reg_eb4: int = 0
    reg_ebc: int = 0
    adda_backup: list[int] = field(default_factory=lambda: [0] * 16)
    iqk_mac_backup: list[int] = field(default_factory=lambda: [0] * C.IQK_MAC_REG_NUM)
    iqk_bb_backup: list[int] = field(default_factory=lambda: [0] * 10)


@dataclass
class RtlDm:
    rfpath_rxenable: list[bool] = field(default_factory=lambda: [False] * 4)
    dm_initialgain_enable: bool = False
    dm_flag: int = 0
    dm_type: int = 0
    disable_framebursting: bool = False
    thermalvalue: int = 0
    undec_sm_pwdb: int = 0
    undec_sm_cck: int = 0
    entry_min_undec_sm_pwdb: int = 0
    entry_max_undec_sm_pwdb: int = 0
    dynamic_txpower_enable: bool = False
    last_dtp_lvl: int = 0
    dynamic_txhighpower_lvl: int = 0
    powerindex_backup: list[int] = field(default_factory=lambda: [0] * 6)
    current_turbo_edca: bool = False
    is_any_nonbepkts: bool = False
    is_cur_rdlstate: bool = False
    useramask: bool = False
    txpower_tracking: bool = False
    txpower_track_control: bool = False
    thermalvalue_lck: int = 0
    thermalvalue_iqk: int = 0
    ofdm_index: list[int] = field(default_factory=lambda: [0] * C.MAX_RF_PATH)
    cck_index: int = 0
    cck_inch14: bool = False
    txpower_trackinginit: bool = False
    tm_trigger: int = 0
    ofdm_pkt_cnt: int = 0
    dm_rssi_sel: int = 0
    last_txok_cnt: int = 0
    last_rxok_cnt: int = 0
    last_bt_edca_ul: int = 0
    last_bt_edca_dl: int = 0
    initial_gain_multi_sta_initialized: bool = False


@dataclass
class DigT:
    """struct dig_t (wifi.h), the fields rtl_dm_diginit sets."""
    dig_enable_flag: bool = False
    dig_ext_port_stage: int = 0
    cur_igvalue: int = 0
    pre_igvalue: int = 0
    cursta_cstate: int = 0
    rssi_val_min: int = 0
    cur_sta_cstate: int = 0
    presta_cstate: int = 0
    curmultista_cstate: int = 0
    rssi_lowthresh: int = 0
    rssi_highthresh: int = 0
    fa_lowthresh: int = 0
    fa_highthresh: int = 0
    rx_gain_max: int = 0
    rx_gain_min: int = 0
    back_val: int = 0
    back_range_max: int = 0
    back_range_min: int = 0
    pre_cck_cca_thres: int = 0
    cur_cck_cca_thres: int = 0
    forbidden_igi: int = 0
    large_fa_hit: int = 0
    recover_cnt: int = 0
    dig_min_0: int = 0
    dig_min_1: int = 0
    media_connect_0: bool = False
    media_connect_1: bool = False
    bt30_cur_igi: int = 0
    pre_cck_pd_state: int = 0
    cur_cck_pd_state: int = 0
    pre_cck_fa_state: int = 0
    cur_cck_fa_state: int = 0


@dataclass
class FalseAlarmStatistics:
    """struct false_alarm_statistics (wifi.h)."""
    cnt_parity_fail: int = 0
    cnt_rate_illegal: int = 0
    cnt_crc8_fail: int = 0
    cnt_mcs_fail: int = 0
    cnt_fast_fsync_fail: int = 0
    cnt_sb_search_fail: int = 0
    cnt_ofdm_fail: int = 0
    cnt_cck_fail: int = 0
    cnt_all: int = 0


@dataclass
class RateAdaptive:
    ratr_state: int = 0
    pre_ratr_state: int = 0


@dataclass
class PsT:
    """struct ps_t (wifi.h): rtlpriv->dm_pstable."""
    pre_ccastate: int = 0
    cur_ccasate: int = 0
    pre_rfstate: int = 0
    cur_rfstate: int = 0
    rssi_val_min: int = 0


@dataclass
class RtlQosParameters:
    """struct rtl_qos_parameters (wifi.h:1057)."""
    cw_min: int = 0
    cw_max: int = 0
    aifs: int = 0
    flag: int = 0
    tx_op: int = 0


@dataclass
class RtlMac:
    mac_addr: bytes = bytes(6)
    vif: bytes | None = None
    link_state: int = C.MAC80211_NOLINK
    opmode: int = C.NL80211_IFTYPE_UNSPECIFIED
    act_scanning: bool = False
    ht_enable: bool = False
    mode: int = 0
    slot_time: int = 0
    retry_long: int = 0
    retry_short: int = 0
    bw_40: bool = False
    bw_80: bool = False
    cur_80_prime_sc: int = 0
    ac: list[RtlQosParameters] = field(default_factory=lambda: [RtlQosParameters() for _ in range(C.AC_MAX)])
    rx_conf: int = 0
    rx_mgt_filter: int = 0
    rx_ctrl_filter: int = 0
    rx_data_filter: int = 0
    cur_40_prime_sc: int = C.HAL_PRIME_CHNL_OFFSET_DONT_CARE
    beacon_interval: int = 0
    min_space_cfg: int = 0
    max_mss_density: int = 0
    current_ampdu_density: int = 0
    current_ampdu_factor: int = 0


@dataclass
class RtlPsCtl:
    rfpwr_state: int = C.ERFON
    rfchange_inprogress: bool = False
    fw_current_inpsmode: bool = False
    p2p_ps_mode: int = 0
    inactiveps: bool = False
    leisure_ps: bool = False
    fwctrl_lps: bool = False
    reg_fwctrl_lps: int = 0
    reg_max_lps_awakeintvl: int = 0
    fwctrl_psmode: int = 0


@dataclass
class RtlSecurity:
    use_sw_sec: bool = False
    use_defaultkey: bool = False


@dataclass
class RtlBtCoexist:
    """struct bt_coexist_info (wifi.h): never set by rtl8192cu."""
    bt_coexistence: bool = False
    bt_coexist_type: int = 0
    bt_edca_ul: int = 0
    bt_edca_dl: int = 0


@dataclass
class RtlStats:
    txbytesunicast: int = 0
    rxbytesunicast: int = 0


@dataclass
class RtlLedCtl:
    led_opendrain: bool = False


@dataclass
class RtlEfuse:
    autoload_failflag: bool = False
    epromtype: int = C.EEPROM_93C46
    efuse_map: list[bytearray] = field(
        default_factory=lambda: [bytearray(C.EFUSE_MAX_LOGICAL_SIZE), bytearray(C.EFUSE_MAX_LOGICAL_SIZE)])
    efuse_usedbytes: int = 0
    efuse_usedpercentage: int = 0
    eeprom_vid: int = 0
    eeprom_did: int = 0
    eeprom_svid: int = 0
    eeprom_smid: int = 0
    eeprom_oemid: int = 0
    eeprom_channelplan: int = 0
    eeprom_version: int = 0
    channel_plan: int = 0
    board_type: int = 0
    external_pa: int = 0
    dev_addr: bytes = bytes(6)
    txpwr_fromeprom: bool = False
    eeprom_tssi: list[int] = field(default_factory=lambda: [0, 0])
    eeprom_pwrlimit_ht20: list[int] = field(default_factory=lambda: [0] * C.CHANNEL_GROUP_MAX)
    eeprom_pwrlimit_ht40: list[int] = field(default_factory=lambda: [0] * C.CHANNEL_GROUP_MAX)
    eeprom_chnlarea_txpwr_cck: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    eeprom_chnlarea_txpwr_ht40_1s: list[list[int]] = field(
        default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    eprom_chnl_txpwr_ht40_2sdf: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_GROUP_MAX))
    pwrgroup_ht20: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    pwrgroup_ht40: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_cck: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_ht40_1s: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwrlevel_ht40_2s: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwr_ht20diff: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    txpwr_legacyhtdiff: list[list[int]] = field(default_factory=lambda: _rf_path_rows(C.CHANNEL_MAX_NUMBER_2G))
    legacy_ht_txpowerdiff: int = 0
    eeprom_regulatory: int = 0
    eeprom_thermalmeter: int = 0
    thermalmeter: list[int] = field(default_factory=lambda: [0, 0])
    apk_thermalmeterignore: bool = False


@dataclass
class RtlUsb:
    in_ep: int = 0
    in_ep_nums: int = 0
    out_ep_nums: int = 0
    out_eps: list[int] = field(default_factory=lambda: [0] * C.RTL_USB_MAX_BULKOUT_NUM)
    out_queue_sel: int = 0
    ep_mapping: list[int] = field(default_factory=lambda: [0] * C.RTL_TXQ_NUM)
    max_bulk_out_size: int = 0
    rx_max_size: int = 0
    rx_urb_num: int = 0
    acm_method: int = 0
    irq_mask: list[int] = field(default_factory=lambda: [0, 0])
    disablehwsm: bool = False
    reg_bcn_ctrl_val: int = 0


@dataclass(frozen=True)
class RtlHalOps:
    """struct rtl_hal_ops: the cfg->ops entries the ported code dispatches through."""
    hw_init: Callable[[RtlPriv], int]
    set_network_type: Callable[[RtlPriv, int], int]
    set_chk_bssid: Callable[[RtlPriv, bool], None]
    get_hw_reg: Callable[[RtlPriv, int], int | bool]
    set_hw_reg: Callable[[RtlPriv, int, int | bytes], None]
    set_channel_access: Callable[[RtlPriv], None]
    set_bw_mode: Callable[[RtlPriv, int], None]
    switch_channel: Callable[[RtlPriv], int]
    dm_watchdog: Callable[[RtlPriv], None]
    get_bbreg: Callable[[RtlPriv, int, int], int]
    set_bbreg: Callable[[RtlPriv, int, int, int], None]
    get_rfreg: Callable[[RtlPriv, int, int, int], int]
    set_rfreg: Callable[[RtlPriv, int, int, int, int], None]
    phy_rf6052_config: Callable[[RtlPriv], bool]
    phy_rf6052_set_cck_txpower: Callable[[RtlPriv, list[int]], None]
    phy_rf6052_set_ofdm_txpower: Callable[[RtlPriv, list[int], int], None]
    config_bb_with_headerfile: Callable[[RtlPriv, int], bool]
    config_bb_with_pgheaderfile: Callable[[RtlPriv, int], bool]
    phy_lc_calibrate: Callable[[RtlPriv, bool], None]
    phy_set_bw_mode_callback: Callable[[RtlPriv], None]


@dataclass(frozen=True)
class RtlModParams:
    sw_crypto: bool = False
    disable_watchdog: bool = False


@dataclass(frozen=True)
class RtlHalCfg:
    """struct rtl_hal_cfg (sw.c rtl92cu_hal_cfg)."""
    name: str
    alt_fw_name: str
    ops: RtlHalOps
    mod_params: RtlModParams


def _rtl92cu_hal_cfg() -> RtlHalCfg:
    from .sw import rtl92cu_hal_cfg
    return rtl92cu_hal_cfg


@dataclass
class RtlPriv:
    io: RegisterIo
    cfg: RtlHalCfg = field(default_factory=_rtl92cu_hal_cfg)
    rtlhal: RtlHal = field(default_factory=RtlHal)
    phy: RtlPhy = field(default_factory=RtlPhy)
    dm: RtlDm = field(default_factory=RtlDm)
    dm_digtable: DigT = field(default_factory=DigT)
    dm_pstable: PsT = field(default_factory=PsT)
    falsealm_cnt: FalseAlarmStatistics = field(default_factory=FalseAlarmStatistics)
    btcoexist: RtlBtCoexist = field(default_factory=RtlBtCoexist)
    stats: RtlStats = field(default_factory=RtlStats)
    ra: RateAdaptive = field(default_factory=RateAdaptive)
    mac80211: RtlMac = field(default_factory=RtlMac)
    psc: RtlPsCtl = field(default_factory=RtlPsCtl)
    sec: RtlSecurity = field(default_factory=RtlSecurity)
    ledctl: RtlLedCtl = field(default_factory=RtlLedCtl)
    efuse: RtlEfuse = field(default_factory=RtlEfuse)
    usb: RtlUsb = field(default_factory=RtlUsb)
    max_fw_size: int = 0
    reg_init: bool = False
    reg_874: int = 0
    reg_c70: int = 0
    reg_85c: int = 0
    reg_a74: int = 0
