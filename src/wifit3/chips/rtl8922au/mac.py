"""RTL8922AU MAC helpers, ported from rtw89-7.2 (mac.c, mac_be.c, core.c, rtw8922a.c)."""

import time

from .constants import (
    R_AX_SYS_CFG1, R_BE_SYS_CHIPINFO, R_AX_WLAN_XTAL_SI_CTRL,
    B_AX_CHIP_VER_MASK, B_BE_HW_ID_MASK,
    B_AX_WL_XTAL_SI_ADDR_MASK, B_AX_WL_XTAL_SI_DATA_MASK, B_AX_WL_XTAL_SI_MODE_MASK,
    B_AX_WL_XTAL_SI_BITMASK_MASK, B_AX_WL_XTAL_SI_CMD_POLL,
    XTAL_SI_NORMAL_READ, XTAL_SI_NORMAL_WRITE, XTAL_SI_POLL_ATTEMPTS,
    XTAL_SI_CV, XTAL_SI_ACV_MASK, XTAL_SI_CHIP_ID_L, XTAL_SI_CHIP_ID_H,
    XTAL_SI_PLL, XTAL_SI_PLL_1, XTAL_SI_ANAPAR_WL, XTAL_SI_WL_RFC_S0, XTAL_SI_WL_RFC_S1,
    XTAL_SI_XREF_RF1, XTAL_SI_XREF_RF2, XTAL_SI_SRAM_CTRL, XTAL_SI_SRAM_DIS,
    CHIP_CAV, PWR_POLL_ATTEMPTS,
    R_AX_GPIO_MUXCFG, B_AX_BOOT_MODE, R_AX_SYS_PW_CTRL, B_AX_APFN_ONMAC,
    R_AX_SYS_STATUS1, B_AX_AUTO_WLPON, R_AX_RSV_CTRL, B_AX_R_DIS_PRST,
    R_BE_SYSON_FSM_MON, WLAN_FSM_MASK, WLAN_FSM_SET, WLAN_FSM_STATE_MASK, WLAN_FSM_IDLE,
    R_BE_IC_PWR_STATE, B_BE_WLMAC_PWR_STE_MASK, MAC_AX_MAC_OFF, MAC_AX_MAC_ON, MAC_AX_MAC_LPS,
    R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN, B_BE_HAXIDMA_IO_ST, B_BE_HAXIDMA_BACKUP_RESTORE_ST,
    B_BE_HCI_WLAN_IO_EN, B_BE_HCI_WLAN_IO_ST,
    R_BE_SYS_PW_CTRL, B_BE_EN_WLON, B_BE_APFM_SWLPS, B_BE_APFM_OFFMAC,
    R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS,
    B_BE_AFSM_WLSUS_EN, B_BE_AFSM_PCIE_SUS_EN, B_BE_DIS_WLBT_PDNSUSEN_SOPC,
    B_BE_DIS_WLBT_LPSEN_LOPC, B_BE_APDM_HPDN, B_BE_RDY_SYSPWR,
    R_BE_WLRESUME_CTRL, B_BE_LPSROP_CMAC0, B_BE_LPSROP_CMAC1, B_BE_APFN_ONMAC,
    R_BE_AFE_ON_CTRL1, B_BE_REG_CK_MON_CK960M_EN,
    R_BE_ANAPAR_POW_MAC, B_BE_POW_PC_LDO_PORT0, B_BE_POW_PC_LDO_PORT1,
    R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_ADDA_P02PP, B_BE_R_SYM_ISO_ADDA_P12PP,
    B_BE_FEN_BB_IP_RSTN, B_BE_FEN_BBPLAT_RSTB,
    R_BE_PLATFORM_ENABLE, B_BE_PLATFORM_EN,
    R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC1_1P3, B_BE_SYM_PADPDN_WL_RFC0_1P3,
    R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK,
    R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE, B_BE_PWC_EV2EF_B, B_BE_PWC_EV2EF_S,
    R_BE_DMAC_FUNC_EN, B_BE_MAC_FUNC_EN, B_BE_DMAC_FUNC_EN, B_BE_MPDU_PROC_EN,
    B_BE_WD_RLS_EN, B_BE_DLE_WDE_EN, B_BE_TXPKT_CTRL_EN, B_BE_STA_SCH_EN, B_BE_DLE_PLE_EN,
    B_BE_PKT_BUF_EN, B_BE_DMAC_TBL_EN, B_BE_PKT_IN_EN, B_BE_DLE_CPUIO_EN, B_BE_DISPATCHER_EN,
    B_BE_BBRPT_EN, B_BE_MAC_SEC_EN, B_BE_H_AXIDMA_EN, B_BE_DMAC_MLO_EN, B_BE_PLRLS_EN,
    B_BE_P_AXIDMA_EN, B_BE_DLE_DATACPUIO_EN,
    R_BE_CMAC_SHARE_FUNC_EN, B_BE_CMAC_SHARE_EN, B_BE_RESPBA_EN, B_BE_ADDRSRCH_EN, B_BE_BTCOEX_EN,
    R_BE_CMAC_FUNC_EN, B_BE_CMAC_EN, B_BE_CMAC_TXEN, B_BE_CMAC_RXEN, B_BE_SIGB_EN,
    B_BE_PHYINTF_EN, B_BE_CMAC_DMA_EN, B_BE_PTCLTOP_EN, B_BE_SCHEDULER_EN, B_BE_TMAC_EN,
    B_BE_RMAC_EN, B_BE_TXTIME_EN, B_BE_RESP_PKTCTL_EN,
    R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN, B_BE_WHOLE_SYS_PWR_STE_MASK, MAC_AX_SYS_ACT,
    R_BE_EFUSE_CTRL, B_BE_EF_ADDR_MASK, B_BE_EF_RDY, R_BE_EFUSE_CTRL_1_V1,
    R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST,
    EF_FV_OFSET_BE_V1, EF_CV_MASK, EF_CV_INV,
    EFUSE_SEC_BE_START, EFUSE_SEC_BE_SIZE, EFUSE_SB_CRYP_SEL_ADDR, EFUSE_SB_CRYP_SEL_DEFAULT,
    R_BE_SCOREBOARD, MAC_AX_NOTIFY_TP_MAJOR, MAC_AX_NOTIFY_PWR_MAJOR,
    B_BE_SOP_EASWR, B_BE_XTAL_OFF_A_DIE,
    R_BE_GPIO8_15_FUNC_SEL, B_BE_PINMUX_GPIO9_FUNC_SEL_MASK, RFKILL_PINMUX_GPIO9_DATA,
    R_BE_GPIO_EXT_CTRL, B_BE_GPIO_MOD_9, B_BE_GPIO_IO_SEL_9, B_BE_GPIO_IN_9,
    RTW89_MAC_0, RTW89_MAC_1, RTW89_MAC_BE_BAND_REG_OFFSET, R_BE_AFE_CTRL1,
    B_BE_R_SYM_WLCMAC0_ALL_EN, B_BE_R_SYM_WLCMAC1_ALL_EN,
    B_BE_R_SYM_ISO_CMAC02PP, B_BE_R_SYM_ISO_CMAC12PP, B_BE_CMAC0_FEN, B_BE_CMAC1_FEN,
    R_BE_CK_EN, B_BE_CK_EN_SET, B_BE_CMAC_FUNC_EN_SET,
    RTW89_PHY_0, RTW89_PHY_1, R_BE_DMAC_SYS_CR32B, B_BE_DMAC_BB_PHY0_MASK, B_BE_DMAC_BB_PHY1_MASK,
    B_BE_FEN_BB1_IP_RSTN, B_BE_FEN_BB1PLAT_RSTB, B_BE_BOOT_RDY0, B_BE_BOOT_RDY1,
    R_BE_MEM_PWR_CTRL, B_BE_MEM_BBMCU0_DS_V1, RTW89_BBMCU_ADDR_OFFSET, BB_MCU_INIT_REG,
    R_BE_HCI_FUNC_EN, B_BE_HCI_TXDMA_EN, B_BE_HCI_RXDMA_EN,
    R_BE_HAXI_INIT_CFG1, B_BE_DMA_MODE_MASK, S_BE_DMA_MOD_USB, B_BE_STOP_AXI_MST,
    B_BE_TXDMA_EN, B_BE_RXDMA_EN, R_BE_HAXI_DMA_STOP1, B_BE_TX_STOP1_MASK,
    R_BE_DMAC_TABLE_CTRL, B_BE_DMAC_ADDR_MODE,
    R_BE_DMAC_CLK_EN, B_BE_DLE_WDE_CLK_EN, B_BE_DLE_PLE_CLK_EN,
    R_BE_WDE_PKTBUF_CFG, R_BE_PLE_PKTBUF_CFG,
    B_BE_WDE_PAGE_SEL_MASK, B_BE_WDE_START_BOUND_MASK, B_BE_WDE_FREE_PAGE_NUM_MASK,
    B_BE_PLE_PAGE_SEL_MASK, B_BE_PLE_START_BOUND_MASK, B_BE_PLE_FREE_PAGE_NUM_MASK,
    S_AX_WDE_PAGE_SEL_64, S_AX_PLE_PAGE_SEL_128, S_AX_PLE_PAGE_SEL_256, DLE_BOUND_UNIT,
    WDE_SIZE8_LNK_PGE_NUM, WDE_SIZE8_SRT_OFST, PLE_SIZE7_LNK_PGE_NUM, PLE_SIZE7_SRT_OFST,
    WDE_QT8_V1, PLE_QT14_V1, PLE_QT15_V1,
    R_BE_WDE_QTA0_CFG, R_BE_PLE_QTA0_CFG, B_BE_QTA_MIN_SIZE_MASK, B_BE_QTA_MAX_SIZE_MASK,
    R_AX_WDE_INI_STATUS, R_AX_PLE_INI_STATUS, WDE_MGN_INI_RDY, PLE_MGN_INI_RDY,
    WDE_SIZE3_LNK_PGE_NUM, WDE_SIZE3_SRT_OFST, PLE_SIZE3_LNK_PGE_NUM, PLE_SIZE3_SRT_OFST,
    PLE_QT9, EXT_WDE_MIN_QT_WCPU,
    R_BE_HCI_FC_CTRL, B_BE_HCI_FC_EN, B_BE_HCI_FC_CH12_EN,
    R_BE_CH_PAGE_CTRL, B_BE_PREC_PAGE_CH12_V1_MASK, HFC_H2C_PREC,
    R_BE_CH0_PAGE_CTRL, R_BE_CH0_PAGE_INFO, R_BE_PUB_PAGE_CTRL1, R_BE_PUB_PAGE_CTRL2,
    R_BE_PUB_PAGE_INFO1, R_BE_PUB_PAGE_INFO2, R_BE_PUB_PAGE_INFO3,
    R_BE_WP_PAGE_CTRL1, R_BE_WP_PAGE_CTRL2, R_BE_WP_PAGE_INFO1,
    B_AX_MAX_PG_MASK, B_AX_MIN_PG_MASK, B_AX_GRP, B_AX_PUBPG_G0_MASK, B_AX_PUBPG_G1_MASK,
    B_AX_WP_THRD_MASK, B_BE_PREC_PAGE_CH011_V1_MASK, B_BE_PUBPG_ALL_MASK,
    B_BE_PREC_PAGE_WP_CH07_MASK, B_BE_PREC_PAGE_WP_CH811_MASK,
    B_BE_HCI_FC_CH12_FULL_COND_MASK, B_BE_HCI_FC_WP_CH811_FULL_COND_MASK,
    B_BE_HCI_FC_WP_CH07_FULL_COND_MASK, B_BE_HCI_FC_WD_FULL_COND_MASK, B_BE_HCI_FC_MODE_MASK,
    RTW89_HCIFC_STF, RTW89_DMA_H2C, HFC_CH_CFG_CH8, HFC_PUB_CFG_P8, HFC_PREC_CFG_C6,
    R_BE_SS_CTRL, B_BE_SS_INIT_DONE, B_BE_WARM_INIT, B_BE_BAND_TRIG_EN, B_BE_BAND1_TRIG_EN,
    B_BE_SS_EN,
    R_BE_MPDU_PROC, B_BE_APPEND_FCS, R_BE_CUT_AMSDU_CTRL, TRXCFG_MPDU_PROC_CUT_CTRL,
    B_BE_CA_CHK_ADDRCAM_EN, R_BE_HDR_SHCUT_SETTING, B_BE_TX_MAC_MPDU_PROC_EN,
    B_BE_TX_HW_ACK_POLICY_EN, B_BE_TX_HW_SEQ_EN, B_BE_TX_ADDR_MLD_TO_LIK,
    R_BE_RX_HDRTRNS, TRXCFG_MPDU_PROC_RX_HDR_CONV, B_BE_HC_ADDR_HIT_EN, R_BE_DISP_FWD_WLAN_0,
    B_BE_FWD_WLAN_CPU_TYPE_0_DATA_MASK, B_BE_FWD_WLAN_CPU_TYPE_0_MNG_MASK,
    B_BE_FWD_WLAN_CPU_TYPE_0_CTL_MASK, B_BE_FWD_WLAN_CPU_TYPE_1_MASK,
    R_BE_SEC_ENG_CTRL, B_BE_SEC_PRE_ENQUE_TX, B_BE_CLK_EN_CGCMP, B_BE_CLK_EN_WAPI,
    B_BE_CLK_EN_WEP_TKIP, B_BE_BMC_MGNT_DEC, B_BE_UC_MGNT_DEC, B_BE_MC_DEC, B_BE_BC_DEC,
    B_BE_SEC_RX_DEC, B_BE_SEC_TX_ENC, R_BE_SEC_MPDU_PROC, B_BE_APPEND_ICV, B_BE_APPEND_MIC,
    R_BE_TXPKTCTL_MPDUINFO_CFG, B_BE_MPDUINFO_FEN, B_BE_MPDUINFO_PKTID_MASK,
    B_BE_MPDUINFO_B1_BADDR_MASK, MPDU_INFO_B1_OFST,
    R_BE_MLO_INIT_CTL, B_BE_MLO_TABLE_INIT_DONE, B_BE_MLO_TABLE_REINIT, B_BE_MLO_HW_CHGLINK_EN,
    R_BE_CMAC_SHARE_ACQCHK_CFG_0, B_BE_R_MACID_ACQ_CHK_EN,
    R_BE_HE_CTN_CHK_CCA_NAV, B_BE_HE_CTN_CHK_TX_NAV, B_BE_HE_CTN_CHK_INTRA_NAV,
    B_BE_HE_CTN_CHK_BASIC_NAV, B_BE_HE_CTN_CHK_NO_GNT_WL, B_BE_HE_CTN_CHK_EDCCA_BITMAP,
    B_BE_HE_CTN_CHK_CCA_BITMAP, B_BE_HE_CTN_CHK_EDCCA_P20, B_BE_HE_CTN_CHK_CCA_P20,
    R_BE_HE_SIFS_CHK_CCA_NAV, B_BE_HE_SIFS_CHK_NO_GNT_WL, B_BE_HE_SIFS_CHK_EDCCA_BITMAP,
    B_BE_HE_SIFS_CHK_EDCCA_P20, R_BE_TB_CHK_CCA_NAV, B_BE_TB_CHK_BASIC_NAV, B_BE_TB_CHK_NO_GNT_WL,
    B_BE_TB_CHK_EDCCA_BITMAP, R_BE_CCA_CFG_0, B_BE_NO_GNT_WL_EN, R_BE_EDCA_BCNQ_PARAM,
    B_BE_BCNQ_CW_MASK, B_BE_BCNQ_AIFS_MASK, BCN_IFS_25US,
    R_BE_ADDR_CAM_CTRL, B_BE_ADDR_CAM_RANGE_MASK, ADDR_CAM_SERCH_RANGE, B_BE_ADDR_CAM_CLR,
    B_BE_ADDR_CAM_EN,
    R_BE_MGNT_FLTR, R_BE_CTRL_FLTR, R_BE_DATA_FLTR, RX_FLTR_FRAME_ACCEPT_BE, R_BE_RX_FLTR_OPT,
    B_BE_UID_FILTER_MASK, B_BE_A_BC_CAM_MATCH, B_BE_A_UC_CAM_MATCH, B_BE_A_MC, B_BE_A_BC,
    B_BE_A_A1_MATCH, B_BE_SNIFFER_MODE, R_BE_PLCP_HDR_FLTR, B_BE_HE_SIGB_CRC_CHK,
    B_BE_VHT_MU_SIGB_CRC_CHK, B_BE_VHT_SU_SIGB_CRC_CHK, B_BE_SIGA_CRC_CHK, B_BE_LSIG_PARITY_CHK_EN,
    B_BE_CCK_SIG_CHK, B_BE_CCK_CRC_CHK,
    R_BE_WMAC_NAV_CTL, B_BE_WMAC_NAV_UPPER_EN, B_BE_WMAC_PLCP_UP_NAV_EN, B_BE_WMAC_TF_UP_NAV_EN,
    B_BE_WMAC_NAV_UPPER_MASK, NAV_25MS, R_BE_SPECIAL_TX_SETTING, B_BE_BMC_NAV_PROTECT,
    R_BE_TRXPTCL_RESP_0, B_BE_WMAC_MBA_DUR_FORCE,
    R_BE_RX_SR_CTRL, B_BE_SR_CTRL_PLCP_EN, B_BE_SR_EN, R_BE_BSSID_SRC_CTRL, B_BE_PLCP_SRC_EN,
    R_BE_TB_PPDU_CTRL, B_BE_QOSNULL_UPD_MUEDCA_EN, R_BE_WMTX_TCR_BE_4,
    B_BE_EHT_HE_PPDU_4XLTF_ZLD_USTIMER_MASK, B_BE_EHT_HE_PPDU_2XLTF_ZLD_USTIMER_MASK,
    R_BE_MAC_LOOPBACK, S_BE_MACLBK_PLCP_DLY_DEF, B_BE_MACLBK_PLCP_DLY_MASK, B_BE_MACLBK_EN,
    WMAC_SPEC_SIFS_CCK, B_BE_WMAC_SPEC_SIFS_CCK_MASK,
    WMAC_SPEC_SIFS_OFDM_1115E, B_BE_WMAC_SPEC_SIFS_OFDM_MASK, R_BE_WMAC_ACK_BA_RESP_LEGACY,
    B_BE_ACK_BA_RESP_LEGACY_CHK_EDCCA, R_BE_WMAC_ACK_BA_RESP_HE, B_BE_ACK_BA_RESP_HE_CHK_EDCCA,
    R_BE_WMAC_ACK_BA_RESP_EHT_LEG_PUNC, B_BE_ACK_BA_EHT_LEG_PUNC_CHK_EDCCA, R_BE_RXTRIG_TEST_USER_2,
    B_BE_RXTRIG_FCSCHK_EN, R_BE_TRXPTCL_RESP_1, B_BE_FTM_RRSR_RATE_EN_MASK,
    B_BE_WMAC_RESP_DOPPLEB_BE_EN, B_BE_WMAC_RESP_DCM_EN, B_BE_WMAC_RESP_REF_RATE_SEL,
    B_BE_WMAC_RESP_REF_RATE_MASK, R_BE_PTCL_RRSR1, B_BE_RRSR_RATE_EN_MASK, B_BE_RRSR_CCK_MASK,
    B_BE_RSC_MASK, R_BE_PTCL_RRSR0, B_BE_RRSR_OFDM_MASK, B_BE_RRSR_HT_MASK, B_BE_RRSR_VHT_MASK,
    B_BE_RRSR_HE_MASK,
    R_BE_RESPBA_CAM_CTRL, B_BE_BACAM_RST_MASK, S_BE_BACAM_RST_DONE, S_BE_BACAM_RST_ALL,
    R_BE_DLK_PROTECT_CTL, B_BE_RX_DLK_CCA_TIME_MASK, TRXCFG_RMAC_CCA_TO, B_BE_RX_DLK_DATA_TIME_MASK,
    TRXCFG_RMAC_DATA_TO, B_BE_RX_DLK_RST_EN, B_BE_RX_MPDU_MAX_LEN_MASK, R_BE_RCR, B_BE_BUSY_CHKSN,
    R_BE_RX_PLCP_EXT_OPTION_1, B_BE_PLCP_SU_PSDU_LEN_SRC, PLD_RLS_MAX_PG, RX_MAX_LEN_UNIT,
    RX_SPEC_MAX_LEN, RTW89_PLE_PG_256,
    PLE_RSVD_QT2, R_BE_RESP_CSI_RESERVED_PAGE, B_BE_CSI_RESERVED_PAGE_NUM_MASK,
    B_BE_CSI_RESERVED_START_PAGE_MASK, R_BE_TX_SUB_BAND_VALUE, B_BE_TXSB_160M_MASK,
    B_BE_TXSB_80M_MASK, B_BE_TXSB_40M_MASK, B_BE_TXSB_20M_MASK, S_BE_TXSB_160M_1, S_BE_TXSB_80M_2,
    S_BE_TXSB_40M_4, S_BE_TXSB_20M_8, R_BE_PTCL_COMMON_SETTING_0, B_BE_PTCL_TRIGGER_SS_EN_UL,
    B_BE_PTCL_TRIGGER_SS_EN_1, B_BE_PTCL_TRIGGER_SS_EN_0, B_BE_CMAC_TX_MODE_1, B_BE_CMAC_TX_MODE_0,
    R_BE_AMPDU_AGG_LIMIT, B_BE_AMPDU_MAX_TIME_MASK, AMPDU_MAX_TIME,
    R_BE_RX_CTRL_1, B_BE_RXDMA_TXRPT_QUEUE_ID_SW_MASK, B_BE_RXDMA_F2PCMDRPT_QUEUE_ID_SW_MASK,
    WLCPU_RXCH2_QID, S_BE_TXSB_160M_0, S_BE_TXSB_80M_0, S_BE_TXSB_40M_1, S_BE_TXSB_20M_2,
    R_BE_PTCL_TX_CTN_SEL, B_BE_PTCL_BUSY, R_BE_PLE_BUFMGN_CTL, B_BE_PLE_AVAL_UPD_REQ,
    B_BE_PLE_AVAL_UPD_QTAID_MASK, PLE_QTAID_B0_TXPL, PLE_QTAID_CMAC0_RX,
    PRELD_AMSDU_SIZE, PRELD_NEXT_MIN_SIZE, PRELD_NEXT_WND, PRELD_B0_ENT_NUM, PRELD_B1_ENT_NUM,
    PRELD_MISCQ_ENT_NUM_8922A, PRELD_B0_ACQ_ENT_NUM_8922A, PRELD_B1_ACQ_ENT_NUM_8922A,
    R_BE_TXPKTCTL_B0_PRELD_CFG0, R_BE_TXPKTCTL_B0_PRELD_CFG1, R_BE_TXPKTCTL_B1_PRELD_CFG0,
    R_BE_TXPKTCTL_B1_PRELD_CFG1, B_BE_B0_PRELD_FEN, B_BE_B0_PRELD_USEMAXSZ_MASK,
    B_BE_B0_PRELD_CAM_G1ENTNUM_MASK, B_BE_B0_PRELD_CAM_G0ENTNUM_MASK,
    B_BE_B0_PRELD_NXT_TXENDWIN_MASK, B_BE_B0_PRELD_NXT_RSVMINSZ_MASK,
    IMR_DMAC_REGS, IMR_CMAC_REGS,
    R_BE_DMAC_ERR_IMR, DMAC_ERR_IMR_EN, B_BE_DMAC_NOTX_ERR_INT_EN, R_BE_CMAC_ERR_IMR,
    R_BE_CMAC_ERR_IMR_C1, CMAC0_ERR_IMR_EN, CMAC1_ERR_IMR_EN, R_BE_WDRLS_CFG, B_BE_WDRLS_MODE_MASK,
    RTW89_RPR_MODE_STF, R_BE_RLSRPT0_CFG0, B_BE_RLSRPT0_QID_MASK, WDRLS_DEST_QID_STF,
    R_BE_RLSRPT0_CFG1, S_BE_WDRLS_FLTR_TXOK, S_BE_WDRLS_FLTR_RTYLMT, S_BE_WDRLS_FLTR_LIFTIM,
    S_BE_WDRLS_FLTR_MACID, B_BE_RLSRPT0_FLTR_MAP_MASK, B_BE_RLSRPT0_TO_MASK, B_BE_RLSRPT0_AGGNUM_MASK,
    R_BE_RSP_CHK_SIG, B_BE_RSP_STATIC_RTS_CHK_SERV_BW_EN, BACAM_1024BMP_OCC_ENTRY,
    R_BE_RXAGG_0_V1, B_BE_RXAGG_0_EN, B_BE_RXAGG_0_NUM_TH, B_BE_RXAGG_0_TIME_32US_TH,
    B_BE_RXAGG_0_BUF_SZ_1K, R_BE_RXAGG_1_V1,
    R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_VALID, B_BE_WCPU_FW_DELAY_COUNT_MASK,
    B_BE_WCPU_EN, B_BE_HOLD_AFTER_RESET,
    R_BE_WCPU_FW_CTRL, B_BE_RUN_ENV_MASK, B_BE_WLANCPU_FWDL_EN, B_BE_BBMCU0_FWDL_EN,
    B_BE_WDT_PLT_RST_EN, B_BE_WCPU_ROM_CUT_GET,
    R_BE_DCPU_PLATFORM_ENABLE, B_BE_DCPU_PLATFORM_EN,
    R_BE_UDM0, R_BE_UDM1, R_BE_UDM2,
    R_BE_HALT_H2C_CTRL, R_BE_HALT_C2H_CTRL, R_BE_HALT_H2C, R_BE_HALT_C2H,
    R_BE_BOOT_DBG, R_BE_HISR0, B_BE_HALT_C2H_INT,
    R_BE_SYS_CLK_CTRL, B_BE_CPU_CLK_EN, R_BE_SYS_CFG5,
    B_BE_WDT_WAKE_PCIE_EN, B_BE_WDT_WAKE_USB_EN, R_BE_SECURE_BOOT_MALLOC_INFO,
    R_BE_GPIO_MUXCFG, B_BE_BOOT_MODE, R_BE_BOOT_REASON, B_BE_BOOT_REASON_MASK,
    R_BE_SYS_WL_EFUSE_CTRL, B_BE_AUTOLOAD_SUS,
    PHYSICAL_EFUSE_SIZE, PHYCAP_ADDR, PHYCAP_SIZE, R_BE_EFUSE_USB_MACADDR, ETH_ALEN,
    RTW89_FWCMD_H2CREG_FUNC_GET_FEATURE, RTW89_H2CREG_GET_FEATURE_PART_NUM,
)
from . import firmware


def field_replace(val: int, mask: int, field: int) -> int:
    """u32_replace_bits: overwrite `mask`'s field of `val` with `field`."""
    return (val & ~mask & 0xFFFFFFFF) | field_prep(mask, field)


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1      # trailing-zero count of the mask


def field_prep(mask: int, val: int) -> int:
    """FIELD_PREP: place `val` into `mask`'s field."""
    return (val << _shift(mask)) & mask


def field_get(mask: int, val: int) -> int:
    """u32_get_bits: extract `mask`'s field from `val`."""
    return (val & mask) >> _shift(mask)


def read_xtal_si(t, offset: int) -> int:
    """rtw89_mac_read_xtal_si_ax: indirect crystal-SI read. Writes an address+read command
    to XTAL_SI_CTRL, polls the same register until the command bit clears, returns the data
    byte. [SRC] mac.c:7208-7234."""
    cmd = (field_prep(B_AX_WL_XTAL_SI_ADDR_MASK, offset)
           | field_prep(B_AX_WL_XTAL_SI_MODE_MASK, XTAL_SI_NORMAL_READ)
           | B_AX_WL_XTAL_SI_CMD_POLL)
    t.write32(R_AX_WLAN_XTAL_SI_CTRL, cmd)
    for _ in range(XTAL_SI_POLL_ATTEMPTS):
        val32 = t.read32(R_AX_WLAN_XTAL_SI_CTRL)
        if not (val32 & B_AX_WL_XTAL_SI_CMD_POLL):
            return field_get(B_AX_WL_XTAL_SI_DATA_MASK, val32)
    return 0


def read_chip_ver(t) -> dict:
    """rtw89_read_chip_ver (BE path): read chip version, analog cut, hw id, and analog id.
    [SRC] core.c:7091-7130. The RTL8852A cv-fixup branch is not on the 8922A graph
    (its guard is chip_id == RTL8852A), so it is not ported here."""
    cv = field_get(B_AX_CHIP_VER_MASK, t.read32(R_AX_SYS_CFG1))
    acv = field_get(XTAL_SI_ACV_MASK, read_xtal_si(t, XTAL_SI_CV))
    cid = field_get(B_BE_HW_ID_MASK, t.read32(R_BE_SYS_CHIPINFO))
    aid = read_xtal_si(t, XTAL_SI_CHIP_ID_L) | (read_xtal_si(t, XTAL_SI_CHIP_ID_H) << 8)
    return {"cv": cv, "acv": acv, "cid": cid, "aid": aid}


def write_xtal_si(t, offset: int, val: int, mask: int) -> None:
    """rtw89_mac_write_xtal_si (BE): indirect crystal-SI write. Writes an address+data+bitmask
    command to XTAL_SI_CTRL, then polls until the command bit clears. [SRC] mac_be.c:413-441.
    The BE field positions match the AX ones (mac.c:7179), so the AX masks apply."""
    cmd = (field_prep(B_AX_WL_XTAL_SI_ADDR_MASK, offset)
           | field_prep(B_AX_WL_XTAL_SI_DATA_MASK, val)
           | field_prep(B_AX_WL_XTAL_SI_BITMASK_MASK, mask)
           | field_prep(B_AX_WL_XTAL_SI_MODE_MASK, XTAL_SI_NORMAL_WRITE)
           | B_AX_WL_XTAL_SI_CMD_POLL)
    t.write32(R_AX_WLAN_XTAL_SI_CTRL, cmd)
    for _ in range(XTAL_SI_POLL_ATTEMPTS):
        if not (t.read32(R_AX_WLAN_XTAL_SI_CTRL) & B_AX_WL_XTAL_SI_CMD_POLL):
            return
    # TODO: verify, untested here. Kernel warns "xtal si not ready(W)". [SRC] mac_be.c:428.


def _poll32(t, addr: int, cond, mask: int = None) -> int:
    """read_poll_timeout over rtw89_read32[_mask]: read `addr` (extracting `mask`'s field when
    given) until `cond(val)` holds. The replay feeds the recorded reads, so the loop stops at
    the same count the kernel did. [SRC] linux/iopoll.h read_poll_timeout."""
    for _ in range(PWR_POLL_ATTEMPTS):
        val = t.read32(addr)
        if mask is not None:
            val = field_get(mask, val)
        if cond(val):
            return val
    raise TimeoutError(f"rtl8922au: poll timeout on 0x{addr:04x}")


def power_switch_boot_mode(t) -> None:
    """rtw89_mac_power_switch_boot_mode: on USB, if the boot-ROM handoff bit is set, clear the
    on-mac / auto-wlpon / boot-mode / prst-disable bits. [SRC] mac.c:1480-1495."""
    if not field_get(B_AX_BOOT_MODE, t.read32(R_AX_GPIO_MUXCFG)):
        return
    t.write32_clr(R_AX_SYS_PW_CTRL, B_AX_APFN_ONMAC)
    t.write32_clr(R_AX_SYS_STATUS1, B_AX_AUTO_WLPON)
    t.write32_clr(R_AX_GPIO_MUXCFG, B_AX_BOOT_MODE)
    t.write32_clr(R_AX_RSV_CTRL, B_AX_R_DIS_PRST)


def reset_pwr_state_be(t) -> None:
    """rtw89_mac_reset_pwr_state_be: force the SYSON FSM to WLAN, wait for it idle, then drive
    the MAC to off from whichever power state it is in. [SRC] mac_be.c:474-601.
    The cold-boot capture comes up MAC_ON; the MAC_OFF and MAC_LPS arms are ported untested."""
    val32 = t.read32(R_BE_SYSON_FSM_MON)
    val32 = (val32 & WLAN_FSM_MASK) | WLAN_FSM_SET
    t.write32(R_BE_SYSON_FSM_MON, val32)
    _poll32(t, R_BE_SYSON_FSM_MON, lambda v: v == WLAN_FSM_IDLE, mask=WLAN_FSM_STATE_MASK)

    state = field_get(B_BE_WLMAC_PWR_STE_MASK, t.read32(R_BE_IC_PWR_STATE))
    if state == MAC_AX_MAC_OFF:
        # TODO: verify, untested here. Cold boot came up MAC_ON. [SRC] mac_be.c:493-516.
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    elif state == MAC_AX_MAC_ON:
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC)
        _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v == MAC_AX_MAC_OFF, mask=B_BE_APFM_OFFMAC)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    elif state == MAC_AX_MAC_LPS:
        # TODO: verify, untested here. Cold boot came up MAC_ON. [SRC] mac_be.c:552-597.
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_set(R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS)
        _poll32(t, R_BE_IC_PWR_STATE, lambda v: v == MAC_AX_MAC_ON, mask=B_BE_WLMAC_PWR_STE_MASK)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC)
        _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v == MAC_AX_MAC_OFF, mask=B_BE_APFM_OFFMAC)
        t.write32_clr(R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)


def pwr_on_func(t, cv: int) -> None:
    """rtw8922a_pwr_on_func: the 8922A MAC power-on register sequence. [SRC] rtw8922a.c:475-634.
    The two PCIE-only blocks are behind the hci-type test; this card is USB, so they never run."""
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_AFSM_WLSUS_EN | B_BE_AFSM_PCIE_SUS_EN)
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_DIS_WLBT_PDNSUSEN_SOPC)
    t.write32_set(R_BE_WLLPS_CTRL, B_BE_DIS_WLBT_LPSEN_LOPC)
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APDM_HPDN)
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v & B_BE_RDY_SYSPWR)

    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
    t.write32_set(R_BE_WLRESUME_CTRL, B_BE_LPSROP_CMAC0 | B_BE_LPSROP_CMAC1)
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFN_ONMAC)
    _poll32(t, R_BE_SYS_PW_CTRL, lambda v: not (v & B_BE_APFN_ONMAC))

    t.write32_clr(R_BE_AFE_ON_CTRL1, B_BE_REG_CK_MON_CK960M_EN)
    t.write8_set(R_BE_ANAPAR_POW_MAC, B_BE_POW_PC_LDO_PORT0 | B_BE_POW_PC_LDO_PORT1)
    t.write32_clr(R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_ADDA_P02PP | B_BE_R_SYM_ISO_ADDA_P12PP)
    t.write8_set(R_BE_PLATFORM_ENABLE, B_BE_PLATFORM_EN)

    # TODO: verify, untested here. PCIE-only HAXIDMA enable+poll. [SRC] rtw8922a.c:510-526.

    t.write32_set(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
    _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: v & B_BE_HCI_WLAN_IO_ST)

    # TODO: verify, untested here. PCIE-only force-ibx clear. [SRC] rtw8922a.c:535-537.

    write_xtal_si(t, XTAL_SI_PLL, 0x02, 0x02)
    write_xtal_si(t, XTAL_SI_PLL, 0x01, 0x01)
    t.write32_set(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC1_1P3)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x40, 0x40)
    t.write32_set(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC0_1P3)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x20, 0x20)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x04, 0x04)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x08, 0x08)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x10)
    write_xtal_si(t, XTAL_SI_WL_RFC_S0, 0xEB, 0xFF)
    write_xtal_si(t, XTAL_SI_WL_RFC_S1, 0xEB, 0xFF)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x01, 0x01)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x02, 0x02)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x80)
    write_xtal_si(t, XTAL_SI_XREF_RF1, 0x00, 0x40)
    write_xtal_si(t, XTAL_SI_XREF_RF2, 0x00, 0x40)
    write_xtal_si(t, XTAL_SI_PLL_1, 0x40, 0x60)
    write_xtal_si(t, XTAL_SI_SRAM_CTRL, 0x00, XTAL_SI_SRAM_DIS)

    if cv != CHIP_CAV:
        t.write32_set(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
        t.write32_set(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
        t.write32_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        time.sleep(0.001)     # mdelay(1). [SRC] rtw8922a.c:600
        t.write32_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
        t.write32_clr(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)

    val32 = (B_BE_MAC_FUNC_EN | B_BE_DMAC_FUNC_EN | B_BE_MPDU_PROC_EN
             | B_BE_WD_RLS_EN | B_BE_DLE_WDE_EN | B_BE_TXPKT_CTRL_EN
             | B_BE_STA_SCH_EN | B_BE_DLE_PLE_EN | B_BE_PKT_BUF_EN
             | B_BE_DMAC_TBL_EN | B_BE_PKT_IN_EN | B_BE_DLE_CPUIO_EN
             | B_BE_DISPATCHER_EN | B_BE_BBRPT_EN | B_BE_MAC_SEC_EN
             | B_BE_H_AXIDMA_EN | B_BE_DMAC_MLO_EN | B_BE_PLRLS_EN
             | B_BE_P_AXIDMA_EN | B_BE_DLE_DATACPUIO_EN)
    # TODO: verify, untested here. PCIE adds B_BE_LTR_CTL_EN. [SRC] rtw8922a.c:613-614.
    t.write32_set(R_BE_DMAC_FUNC_EN, val32)

    t.write32_set(R_BE_CMAC_SHARE_FUNC_EN,
                  B_BE_CMAC_SHARE_EN | B_BE_RESPBA_EN | B_BE_ADDRSRCH_EN | B_BE_BTCOEX_EN)
    t.write32_set(R_BE_CMAC_FUNC_EN,
                  B_BE_CMAC_EN | B_BE_CMAC_TXEN | B_BE_CMAC_RXEN | B_BE_SIGB_EN
                  | B_BE_PHYINTF_EN | B_BE_CMAC_DMA_EN | B_BE_PTCLTOP_EN | B_BE_SCHEDULER_EN
                  | B_BE_TMAC_EN | B_BE_RMAC_EN | B_BE_TXTIME_EN | B_BE_RESP_PKTCTL_EN)

    t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_FEN_BB_IP_RSTN | B_BE_FEN_BBPLAT_RSTB)


def cnv_efuse_state(t, idle: bool) -> None:
    """rtw89_cnv_efuse_state_be: toggle the BT-disable bit around an efuse access, waiting for
    the whole-system power state to go active on entry. [SRC] efuse_be.c:143-162."""
    if idle:
        t.write32_set(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)
    else:
        t.write32_clr(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)
        _poll32(t, R_BE_IC_PWR_STATE, lambda v: v == MAC_AX_SYS_ACT,
                mask=B_BE_WHOLE_SYS_PWR_STE_MASK)


def _enable_efuse_pwr_cut_ddv(t, cv: int) -> None:
    """rtw89_enable_efuse_pwr_cut_ddv_be. The aphy patch runs on every 8922A cut except CAV.
    [SRC] efuse_be.c:23-42."""
    aphy_patch = cv != CHIP_CAV      # chip_id is RTL8922A
    t.write8_set(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
    if aphy_patch:
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
        time.sleep(0.001)            # mdelay(1). [SRC] efuse_be.c:36
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
    t.write32_set(R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST)


def _disable_efuse_pwr_cut_ddv(t, cv: int) -> None:
    """rtw89_disable_efuse_pwr_cut_ddv_be. [SRC] efuse_be.c:44-62."""
    aphy_patch = cv != CHIP_CAV      # chip_id is RTL8922A
    if aphy_patch:
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        time.sleep(0.001)            # mdelay(1). [SRC] efuse_be.c:56
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
    t.write8_clr(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
    t.write32_clr(R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST)


def dump_physical_efuse_map(t, cv: int, dump_addr: int, dump_size: int) -> bytes:
    """rtw89_dump_physical_efuse_map_be (dav=false): read `dump_size` bytes of physical efuse
    from `dump_addr` (both 4-aligned) via the DDV controller, bracketed by the state convert
    and power-cut enable/disable. [SRC] efuse_be.c:64-97, 164-193."""
    cnv_efuse_state(t, False)
    _enable_efuse_pwr_cut_ddv(t, cv)
    out = bytearray()
    for addr in range(dump_addr, dump_addr + dump_size, 4):
        efuse_ctl = field_prep(B_BE_EF_ADDR_MASK, addr)
        t.write32(R_BE_EFUSE_CTRL, efuse_ctl & ~B_BE_EF_RDY & 0xFFFFFFFF)
        _poll32(t, R_BE_EFUSE_CTRL, lambda v: v & B_BE_EF_RDY)
        out += t.read32(R_BE_EFUSE_CTRL_1_V1).to_bytes(4, "little")
    _disable_efuse_pwr_cut_ddv(t, cv)
    cnv_efuse_state(t, True)
    return bytes(out)


def efuse_read_ecv(t, cv: int) -> int:
    """rtw89_efuse_read_ecv_be: read the efuse chip-version nibble and adopt it as the cut,
    unless it reads invalid. [SRC] efuse_be.c:516-540. Returns the cv to use for later access."""
    dump_addr = EF_FV_OFSET_BE_V1 & ~0x3
    buff = dump_physical_efuse_map(t, cv, dump_addr, 4)
    ecv = field_get(EF_CV_MASK, buff[EF_FV_OFSET_BE_V1 & 0x3])
    if ecv == EF_CV_INV:
        return cv
    return ecv


def efuse_read_fw_secure(t, cv: int) -> None:
    """rtw89_efuse_read_fw_secure_be: dump the secure-boot selector. [SRC] efuse_be.c:468-514.
    This card reads the default selector (secure boot off), so the MSS parse is skipped."""
    sec_map = dump_physical_efuse_map(t, cv, EFUSE_SEC_BE_START, EFUSE_SEC_BE_SIZE)
    i = EFUSE_SB_CRYP_SEL_ADDR - EFUSE_SEC_BE_START
    sb_cryp_sel = sec_map[i] | (sec_map[i + 1] << 8)
    if sb_cryp_sel == EFUSE_SB_CRYP_SEL_DEFAULT:
        return
    # TODO: verify, untested here. Secure-boot MSS parse (rtw89_efuse_recognize_mss_info_v1);
    # this card reads the default selector, so it never runs. [SRC] efuse_be.c:490-505.


def update_scoreboard(t, val: int) -> None:
    """rtw89_mac_update_scoreboard: notify the BT-coex firmware over the scoreboard register.
    8922A btc_sb has one entry (R_BE_SCOREBOARD). [SRC] mac.c:1506-1519, rtw8922a.c:3300."""
    t.write8(R_BE_SCOREBOARD + 3, val)


def mac_pwr_on(t, cv: int, probe_done: bool = False) -> None:
    """rtw89_mac_pwr_on -> rtw89_mac_power_switch(on=True): boot-mode handoff, reset the power
    state, run the chip power-on sequence, then the first-probe efuse reads and coex notify.
    [SRC] mac.c:1586-1599, 1521-1568. probe_done marks the interface-up re-power, where
    RTW89_FLAG_PROBE_DONE is set (usb.c:1320), so the efuse-read tail is skipped."""
    power_switch_boot_mode(t)
    reset_pwr_state_be(t)
    pwr_on_func(t, cv)
    # power_switch(on=True) tail. On first probe (RTW89_FLAG_PROBE_DONE unset) the efuse reads
    # run; efuse_read_ecv can update the cut used by the secure-boot dump. [SRC] mac.c:1557-1568.
    if not probe_done:
        cv = efuse_read_ecv(t, cv)
        efuse_read_fw_secure(t, cv)
    update_scoreboard(t, MAC_AX_NOTIFY_TP_MAJOR)
    # rtw89_mac_clr_aon_intr -> clr_aon_intr_be is PCIE-only; a no-op on USB. [SRC] mac_be.c:2064.


def cmac_pwr_en(t, mac_idx: int) -> None:
    """cmac_pwr_en_be(en=True): power the CMAC analog and release its port isolation. Skipped when
    that CMAC is already powered (RTW89_FLAG_CMACn_PWR), so a later sys_init re-call is a no-op.
    [SRC] mac_be.c:804-857."""
    if mac_idx in t.cmac_pwr:
        return
    if mac_idx == RTW89_MAC_0:
        t.write32_set(R_BE_AFE_CTRL1, B_BE_R_SYM_WLCMAC0_ALL_EN)
        t.write32_clr(R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_CMAC02PP)
        t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_CMAC0_FEN)
    else:
        t.write32_set(R_BE_AFE_CTRL1, B_BE_R_SYM_WLCMAC1_ALL_EN)
        t.write32_clr(R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_CMAC12PP)
        t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_CMAC1_FEN)
    t.cmac_pwr.add(mac_idx)


def cmac_func_en(t, mac_idx: int) -> None:
    """cmac_func_en_be(en=True): enable the CMAC clocks then the function bits. reg_by_idx adds
    the band-1 offset for CMAC1. [SRC] mac_be.c:860-900, mac.h:1183-1188."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    t.write32_set(R_BE_CK_EN + off, B_BE_CK_EN_SET)
    t.write32_set(R_BE_CMAC_FUNC_EN + off, B_BE_CMAC_FUNC_EN_SET)


def mac_func_en(t) -> None:
    """rtw89_mac_func_en_be: dmac and cmac-share func-en are no-ops on the 8922A; read
    FEN_RST_ENABLE and, per the CMAC0/1 FEN bits it reports, power then enable those CMACs.
    [SRC] mac_be.c:934-969. The cold-boot capture reports both CMAC0 and CMAC1 enabled."""
    val = t.read32(R_BE_FEN_RST_ENABLE)
    if val & B_BE_CMAC0_FEN:
        cmac_pwr_en(t, RTW89_MAC_0)
        cmac_func_en(t, RTW89_MAC_0)
    if val & B_BE_CMAC1_FEN:
        cmac_pwr_en(t, RTW89_MAC_1)
        cmac_func_en(t, RTW89_MAC_1)


def mac_preinit(t, cv: int) -> None:
    """rtw89_mac_preinit: the interface-up re-power (probe done, so the efuse-read tail is
    skipped) then the 8922A mac_func_en. [SRC] mac.c:4341-4357."""
    mac_pwr_on(t, cv, probe_done=True)
    mac_func_en(t)


def pwr_off_func(t) -> None:
    """rtw8922a_pwr_off_func (USB arm): unwind the analog/xtal config, drive the MAC to off,
    and put the A-die to sleep. [SRC] rtw8922a.c:636-742. The two PCIE-only blocks are behind
    the hci-type test; this card is USB, so they never run."""
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x10, 0x10)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x08)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x04)
    write_xtal_si(t, XTAL_SI_WL_RFC_S0, 0xC6, 0xFF)
    write_xtal_si(t, XTAL_SI_WL_RFC_S1, 0xC6, 0xFF)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x80, 0x80)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x02)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x01)
    write_xtal_si(t, XTAL_SI_PLL, 0x02, 0xFF)
    write_xtal_si(t, XTAL_SI_PLL, 0x00, 0xFF)

    t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_ADDA_P02PP | B_BE_R_SYM_ISO_ADDA_P12PP)
    t.write8_clr(R_BE_ANAPAR_POW_MAC, B_BE_POW_PC_LDO_PORT0 | B_BE_POW_PC_LDO_PORT1)
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
    t.write8_clr(R_BE_FEN_RST_ENABLE, B_BE_FEN_BB_IP_RSTN | B_BE_FEN_BBPLAT_RSTB)
    t.write32_clr(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC0_1P3)

    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x20)
    t.write32_clr(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC1_1P3)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x40)

    # TODO: verify, untested here. PCIE-only HAXIDMA disable+poll. [SRC] rtw8922a.c:691-707.

    t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
    _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not (v & B_BE_HCI_WLAN_IO_ST))
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC)
    _poll32(t, R_BE_SYS_PW_CTRL, lambda v: not (v & B_BE_APFM_OFFMAC))

    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_SOP_EASWR)     # chip_id == RTL8922A, hci == USB
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_XTAL_OFF_A_DIE)
    val32 = t.read32(R_BE_SYS_PW_CTRL)
    val32 = (val32 | B_BE_AFSM_WLSUS_EN) & ~B_BE_AFSM_PCIE_SUS_EN & 0xFFFFFFFF
    t.write32(R_BE_SYS_PW_CTRL, val32)
    t.write32(R_BE_UDM1, 0)


def mac_pwr_off(t) -> None:
    """rtw89_mac_pwr_off -> rtw89_mac_power_switch(on=False): boot-mode handoff (a no-op now the
    boot bit was cleared on power-on), run the chip power-off sequence, then the coex notify.
    [SRC] mac.c:1601-1604, 1521-1584. reset_pwr_state runs only on the power-on path."""
    power_switch_boot_mode(t)
    pwr_off_func(t)
    update_scoreboard(t, MAC_AX_NOTIFY_PWR_MAJOR)


def rfkill_init(t) -> None:
    """rtw89_core_rfkill_init: program the GPIO9 rfkill pinmux and its mode/direction.
    [SRC] core.c:7308-7316, rtw8922a.c:330-337. mode addr is GPIO_EXT_CTRL+2 (the upper 16
    bits), so the mask is shifted down 16."""
    t.write16_mask(R_BE_GPIO8_15_FUNC_SEL, B_BE_PINMUX_GPIO9_FUNC_SEL_MASK, RFKILL_PINMUX_GPIO9_DATA)
    t.write16_mask(R_BE_GPIO_EXT_CTRL + 2, (B_BE_GPIO_MOD_9 | B_BE_GPIO_IO_SEL_9) >> 16, 0)


def rfkill_get(t) -> bool:
    """rtw89_core_rfkill_get: blocked when the GPIO9 input pin reads low.
    [SRC] core.c:7318-7323, rtw8922a.c:3299."""
    return not field_get(B_BE_GPIO_IN_9, t.read8(R_BE_GPIO_EXT_CTRL))


def rfkill_polling_init(t) -> None:
    """rtw89_rfkill_polling_init: set up the rfkill GPIO, do the forced initial poll, then the
    read the kernel's wiphy polling work fires immediately after (a force=false poll that just
    re-reads the pin). [SRC] core.c:7325-7333, mac80211.c:2031. The set_hw_state side is software."""
    rfkill_init(t)
    rfkill_get(t)
    rfkill_get(t)


def ctrl_hci_dma_trx(t, enable: bool) -> None:
    """rtw89_mac_ctrl_hci_dma_trx: toggle HCI TX+RX DMA on hci_func_en_addr (R_BE_HCI_FUNC_EN
    for the 8922A). [SRC] mac.h:1613-1624, rtw8922a.c:3274."""
    bits = B_BE_HCI_TXDMA_EN | B_BE_HCI_RXDMA_EN
    if enable:
        t.write32_set(R_BE_HCI_FUNC_EN, bits)
    else:
        t.write32_clr(R_BE_HCI_FUNC_EN, bits)


def hci_func_en(t) -> None:
    """rtw89_mac_hci_func_en_be: enable HCI TX+RX DMA. [SRC] mac_be.c:369-373."""
    t.write32_set(R_BE_HCI_FUNC_EN, B_BE_HCI_TXDMA_EN | B_BE_HCI_RXDMA_EN)


def dmac_func_pre_en(t) -> None:
    """rtw89_mac_dmac_func_pre_en_be (USB arm): select USB DMA mode, enable TX/RX DMA and the
    AXI master, release the TX stop channels, set DMAC table addressing. [SRC] mac_be.c:375-410.
    Only the USB and RTL8922A branches are taken here; PCIE/SDIO and the _V1 mask are elsewhere."""
    val = t.read32(R_BE_HAXI_INIT_CFG1)
    val = field_replace(val, B_BE_DMA_MODE_MASK, S_BE_DMA_MOD_USB)
    val = (val & ~B_BE_STOP_AXI_MST & 0xFFFFFFFF) | B_BE_TXDMA_EN | B_BE_RXDMA_EN
    t.write32(R_BE_HAXI_INIT_CFG1, val)
    t.write32_clr(R_BE_HAXI_DMA_STOP1, B_BE_TX_STOP1_MASK)   # chip_id == RTL8922A
    t.write32_set(R_BE_DMAC_TABLE_CTRL, B_BE_DMAC_ADDR_MODE)


def dle_func_en(t, enable: bool) -> None:
    """dle_func_en_be: WDE+PLE function enable on DMAC_FUNC_EN. [SRC] mac_be.c:216-224."""
    bits = B_BE_DLE_WDE_EN | B_BE_DLE_PLE_EN
    if enable:
        t.write32_set(R_BE_DMAC_FUNC_EN, bits)
    else:
        t.write32_clr(R_BE_DMAC_FUNC_EN, bits)


def dle_clk_en(t, enable: bool) -> None:
    """dle_clk_en_be: WDE+PLE clock enable (RTL8922A only). [SRC] mac_be.c:226-237."""
    bits = B_BE_DLE_WDE_CLK_EN | B_BE_DLE_PLE_CLK_EN
    if enable:
        t.write32_set(R_BE_DMAC_CLK_EN, bits)
    else:
        t.write32_clr(R_BE_DMAC_CLK_EN, bits)


# dle_mem configs keyed by qta_mode. Each rtw89_dle_size is (page_sel, lnk_pge_num, srt_ofst);
# ext_wcpu None == INVALID_QT_WCPU. [SRC] rtw8922a.c:191-215 (DLFW / USB-2 DBCC).
_DLE_CFG = {
    "DLFW": {
        "wde_size": (S_AX_WDE_PAGE_SEL_64, WDE_SIZE3_LNK_PGE_NUM, WDE_SIZE3_SRT_OFST),
        "ple_size": (S_AX_PLE_PAGE_SEL_128, PLE_SIZE3_LNK_PGE_NUM, PLE_SIZE3_SRT_OFST),
        "wde_qt": (0, 0, 0, 0),          # wde_qt4, all zero
        "ple_min_qt": PLE_QT9, "ple_max_qt": PLE_QT9,
        "ext_wcpu": EXT_WDE_MIN_QT_WCPU,
    },
    "DBCC": {
        "wde_size": (S_AX_WDE_PAGE_SEL_64, WDE_SIZE8_LNK_PGE_NUM, WDE_SIZE8_SRT_OFST),
        "ple_size": (S_AX_PLE_PAGE_SEL_256, PLE_SIZE7_LNK_PGE_NUM, PLE_SIZE7_SRT_OFST),
        "wde_qt": WDE_QT8_V1,
        "ple_min_qt": PLE_QT14_V1, "ple_max_qt": PLE_QT15_V1,
        "ext_wcpu": None,
    },
}


def dle_mix_cfg(t, wde_size, ple_size) -> None:
    """dle_mix_cfg_be: program the WDE/PLE packet-buffer page-select, start bound, and free-page
    count from the mode's rtw89_dle_size pair. [SRC] mac_be.c:239-295."""
    val = t.read32(R_BE_WDE_PKTBUF_CFG)
    val = field_replace(val, B_BE_WDE_PAGE_SEL_MASK, wde_size[0])
    val = field_replace(val, B_BE_WDE_START_BOUND_MASK, wde_size[2] // DLE_BOUND_UNIT)
    val = field_replace(val, B_BE_WDE_FREE_PAGE_NUM_MASK, wde_size[1])
    t.write32(R_BE_WDE_PKTBUF_CFG, val)

    val = t.read32(R_BE_PLE_PKTBUF_CFG)
    val = field_replace(val, B_BE_PLE_PAGE_SEL_MASK, ple_size[0])
    val = field_replace(val, B_BE_PLE_START_BOUND_MASK, ple_size[2] // DLE_BOUND_UNIT)
    val = field_replace(val, B_BE_PLE_FREE_PAGE_NUM_MASK, ple_size[1])
    t.write32(R_BE_PLE_PKTBUF_CFG, val)


def _set_quota(t, reg: int, min_v: int, max_v: int) -> None:
    """SET_QUOTA_VAL: pack a (min, max) size pair into a QTAn_CFG register. [SRC] mac_be.c:315-322."""
    t.write32(reg, field_prep(B_BE_QTA_MIN_SIZE_MASK, min_v)
              | field_prep(B_BE_QTA_MAX_SIZE_MASK, max_v))


def dle_quota_cfg(t, wde_qt, ple_min_qt, ple_max_qt, ext_wcpu) -> None:
    """dle_quota_cfg -> wde_quota_cfg_be + ple_quota_cfg_be. WDE min == max (one struct); the wcpu
    slot takes ext_wcpu when the DLFW ext config gives one, else the struct's own wcpu. 8922A stops
    PLE before snrpt (index 13). [SRC] mac.c:2264-2272, mac_be.c:326-367."""
    min_wcpu = ext_wcpu if ext_wcpu is not None else wde_qt[1]
    max_wcpu = max(wde_qt[1], min_wcpu)
    _set_quota(t, R_BE_WDE_QTA0_CFG + 0 * 4, wde_qt[0], wde_qt[0])   # hif
    _set_quota(t, R_BE_WDE_QTA0_CFG + 1 * 4, min_wcpu, max_wcpu)     # wcpu
    _set_quota(t, R_BE_WDE_QTA0_CFG + 2 * 4, 0, 0)
    _set_quota(t, R_BE_WDE_QTA0_CFG + 3 * 4, wde_qt[2], wde_qt[2])   # pkt_in
    _set_quota(t, R_BE_WDE_QTA0_CFG + 4 * 4, wde_qt[3], wde_qt[3])   # cpu_io
    for i in range(len(ple_min_qt)):
        _set_quota(t, R_BE_PLE_QTA0_CFG + i * 4, ple_min_qt[i], ple_max_qt[i])


def chk_dle_rdy(t, wde_or_ple: bool) -> None:
    """chk_dle_rdy_be: poll WDE (or PLE) init status until the manager-ready bits set.
    [SRC] mac_be.c:297-312."""
    reg = R_AX_WDE_INI_STATUS if wde_or_ple else R_AX_PLE_INI_STATUS
    mask = WDE_MGN_INI_RDY if wde_or_ple else PLE_MGN_INI_RDY
    _poll32(t, reg, lambda v: (v & mask) == mask)


def dle_init(t, mode: str) -> None:
    """rtw89_mac_dle_init: configure the DLE (WDE/PLE) packet-buffer sizes and quotas for the qta
    mode ("DLFW" for firmware download, "DBCC" for the operating BE mode), then wait for the
    managers ready. [SRC] mac.c:2274-2343. check_mac_en is a software-flag test, so no wire op."""
    cfg = _DLE_CFG[mode]
    dle_func_en(t, False)
    dle_clk_en(t, True)
    dle_mix_cfg(t, cfg["wde_size"], cfg["ple_size"])
    dle_quota_cfg(t, cfg["wde_qt"], cfg["ple_min_qt"], cfg["ple_max_qt"], cfg["ext_wcpu"])
    dle_func_en(t, True)
    chk_dle_rdy(t, True)
    chk_dle_rdy(t, False)


def hfc_func_en(t, en: bool, h2c_en: bool) -> None:
    """hfc_func_en_be: toggle the HCI flow-control enable and the CH12 (H2C) enable.
    [SRC] mac_be.c:202-214."""
    val = t.read32(R_BE_HCI_FC_CTRL)
    val = (val | B_BE_HCI_FC_EN) if en else (val & ~B_BE_HCI_FC_EN & 0xFFFFFFFF)
    val = (val | B_BE_HCI_FC_CH12_EN) if h2c_en else (val & ~B_BE_HCI_FC_CH12_EN & 0xFFFFFFFF)
    t.write32(R_BE_HCI_FC_CTRL, val)


def hfc_h2c_cfg(t) -> None:
    """hfc_h2c_cfg_be: program the H2C (CH12) page precedence. [SRC] mac_be.c:152-160."""
    t.write32(R_BE_CH_PAGE_CTRL, field_prep(B_BE_PREC_PAGE_CH12_V1_MASK, HFC_H2C_PREC))


def hfc_ch_ctrl(t, ch: int) -> None:
    """hfc_ch_ctrl: write the DMA channel's (min, max, grp) page config from the ch8 table.
    [SRC] mac.c:972-998."""
    mn, mx, grp = HFC_CH_CFG_CH8[ch]
    val = field_prep(B_AX_MIN_PG_MASK, mn) | field_prep(B_AX_MAX_PG_MASK, mx) | (B_AX_GRP if grp else 0)
    t.write32(R_BE_CH0_PAGE_CTRL + ch * 4, val)


def hfc_pub_ctrl(t) -> None:
    """hfc_pub_ctrl: public group page counts and the write-port threshold. [SRC] mac.c:1027-1051."""
    grp0, grp1, _pub_max, wp_thrd = HFC_PUB_CFG_P8
    t.write32(R_BE_PUB_PAGE_CTRL1,
              field_prep(B_AX_PUBPG_G0_MASK, grp0) | field_prep(B_AX_PUBPG_G1_MASK, grp1))
    t.write32(R_BE_WP_PAGE_CTRL2, field_prep(B_AX_WP_THRD_MASK, wp_thrd))


def hfc_mix_cfg(t) -> None:
    """hfc_mix_cfg_be: page precedence, public max, WP precedence, then the flow-control mode and
    per-queue full-condition fields, from the prec_cfg_c6 / pubcfg_p8 tables. [SRC] mac_be.c:162-200."""
    ch011_prec, h2c_prec, wp07_prec, wp811_prec, ch011_fc, h2c_fc, wp07_fc, wp811_fc = HFC_PREC_CFG_C6
    pub_max = HFC_PUB_CFG_P8[2]
    t.write32(R_BE_CH_PAGE_CTRL, field_prep(B_BE_PREC_PAGE_CH011_V1_MASK, ch011_prec)
              | field_prep(B_BE_PREC_PAGE_CH12_V1_MASK, h2c_prec))
    t.write32(R_BE_PUB_PAGE_CTRL2, field_prep(B_BE_PUBPG_ALL_MASK, pub_max))
    t.write32(R_BE_WP_PAGE_CTRL1, field_prep(B_BE_PREC_PAGE_WP_CH07_MASK, wp07_prec)
              | field_prep(B_BE_PREC_PAGE_WP_CH811_MASK, wp811_prec))
    val = t.read32(R_BE_HCI_FC_CTRL)
    val = field_replace(val, B_BE_HCI_FC_MODE_MASK, RTW89_HCIFC_STF)
    val = field_replace(val, B_BE_HCI_FC_WD_FULL_COND_MASK, ch011_fc)
    val = field_replace(val, B_BE_HCI_FC_CH12_FULL_COND_MASK, h2c_fc)
    val = field_replace(val, B_BE_HCI_FC_WP_CH07_FULL_COND_MASK, wp07_fc)
    val = field_replace(val, B_BE_HCI_FC_WP_CH811_FULL_COND_MASK, wp811_fc)
    t.write32(R_BE_HCI_FC_CTRL, val)


def hfc_upd_ch_info(t, ch: int) -> None:
    """hfc_upd_ch_info: read back the channel's available/used page counts into software state.
    [SRC] mac.c:1000-1024."""
    t.read32(R_BE_CH0_PAGE_INFO + ch * 4)


def hfc_get_mix_info(t) -> None:
    """hfc_get_mix_info_be: read back the public/WP page info and the flow-control config into
    software state. All reads, in the source's order. [SRC] mac_be.c hfc_get_mix_info_be."""
    t.read32(R_BE_PUB_PAGE_INFO1)
    t.read32(R_BE_PUB_PAGE_INFO3)
    t.read32(R_BE_PUB_PAGE_INFO2)
    t.read32(R_BE_WP_PAGE_INFO1)
    t.read32(R_BE_HCI_FC_CTRL)
    t.read32(R_BE_CH_PAGE_CTRL)
    t.read32(R_BE_PUB_PAGE_CTRL2)
    t.read32(R_BE_WP_PAGE_CTRL1)
    t.read32(R_BE_WP_PAGE_CTRL2)
    t.read32(R_BE_PUB_PAGE_CTRL1)


def hfc_init(t, reset: bool, en: bool, h2c_en: bool) -> None:
    """rtw89_mac_hfc_init: reset the flow-control params (software), disable FC, then either the
    H2C-only download path (en=False: program H2C precedence, enable H2C) or the full operating
    path (per-channel + public + mix config, enable, then read the counters back). dma_ch_mask is
    0 on the 8922A so no channel is skipped. [SRC] mac.c:1194-1246."""
    hfc_func_en(t, False, False)
    if not en and h2c_en:
        hfc_h2c_cfg(t)
        hfc_func_en(t, en, h2c_en)
        return
    for ch in range(RTW89_DMA_H2C):
        hfc_ch_ctrl(t, ch)
    hfc_pub_ctrl(t)
    hfc_mix_cfg(t)
    if en or h2c_en:
        hfc_func_en(t, en, h2c_en)          # udelay(10) after: sub-resolution, no wire op
    for ch in range(RTW89_DMA_H2C):
        hfc_upd_ch_info(t, ch)
    hfc_get_mix_info(t)


def fwdl_preconfig(t) -> None:
    """rtw89_mac_fwdl_preconfig_be: clear the WCPU firmware auto-cal delay before download.
    [SRC] mac_be.c:625-629."""
    t.write32_clr(R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_VALID)
    t.write32_mask(R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_MASK, 0)


def disable_cpu(t) -> None:
    """rtw89_mac_disable_cpu_be: park the WLAN CPU (hold-after-reset toggle), clear the run-env
    and AON debug registers before a fresh firmware download. [SRC] mac_be.c:603-623."""
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_HOLD_AFTER_RESET)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32(R_BE_WCPU_FW_CTRL, t.read32(R_BE_WCPU_FW_CTRL) & B_BE_RUN_ENV_MASK)
    t.write32_set(R_BE_DCPU_PLATFORM_ENABLE, B_BE_DCPU_PLATFORM_EN)   # chip_id == RTL8922A
    t.write32(R_BE_UDM0, 0)
    t.write32(R_BE_HALT_C2H, 0)
    t.write32(R_BE_UDM2, 0)


def set_cpu_en(t, include_bb: bool) -> None:
    """set_cpu_en: arm the WLAN CPU (and, when downloading BB MCU, the BBMCU0) for firmware
    download. [SRC] mac_be.c:631-639."""
    bits = B_BE_WLANCPU_FWDL_EN
    if include_bb:
        bits |= B_BE_BBMCU0_FWDL_EN
    t.write32_set(R_BE_WCPU_FW_CTRL, bits)


def wcpu_on(t, boot_reason: int, dlfw: bool) -> None:
    """wcpu_on: clear the boot/halt mailbox state, enable the CPU clock, set the boot reason,
    and pulse the WLAN CPU out of reset. [SRC] mac_be.c:641-698. The pre-boot AON warnings are
    read-only diagnostics; the reads are reproduced. On the 8922A the HOST_EXIST write is
    skipped; with dlfw the trailing FreeRTOS-ready poll is skipped."""
    t.read32(R_BE_HALT_C2H)
    t.read32(R_BE_UDM1)
    t.read32(R_BE_UDM2)
    t.write32(R_BE_UDM1, 0)
    t.write32(R_BE_UDM2, 0)
    t.write32(R_BE_BOOT_DBG, 0)
    t.write32(R_BE_HALT_H2C, 0)
    t.write32(R_BE_HALT_C2H, 0)
    t.write32(R_BE_HALT_H2C_CTRL, 0)
    t.write32(R_BE_HALT_C2H_CTRL, 0)
    t.read32(R_BE_HISR0)
    t.write32(R_BE_HISR0, B_BE_HALT_C2H_INT)
    t.write32_set(R_BE_SYS_CLK_CTRL, B_BE_CPU_CLK_EN)
    t.write32_clr(R_BE_SYS_CFG5, B_BE_WDT_WAKE_PCIE_EN | B_BE_WDT_WAKE_USB_EN)
    t.write32_clr(R_BE_WCPU_FW_CTRL, B_BE_WDT_PLT_RST_EN | B_BE_WCPU_ROM_CUT_GET)
    t.write32(R_BE_SECURE_BOOT_MALLOC_INFO, 0)
    t.write32_clr(R_BE_GPIO_MUXCFG, B_BE_BOOT_MODE)
    # chip_id == RTL8922A: the non-8922A HOST_EXIST set is skipped. [SRC] mac_be.c:683-684.
    t.write16_mask(R_BE_BOOT_REASON, B_BE_BOOT_REASON_MASK, boot_reason)
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_HOLD_AFTER_RESET)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    # dlfw is True on the download path, so the FreeRTOS-ready poll is skipped. [SRC] mac_be.c:691.


def fwdl_enable_wcpu(t, boot_reason: int, dlfw: bool, include_bb: bool) -> None:
    """rtw89_mac_fwdl_enable_wcpu_be: arm the CPU download bits, then boot the WLAN CPU.
    [SRC] mac_be.c:700-707."""
    set_cpu_en(t, include_bb)
    wcpu_on(t, boot_reason, dlfw)


def fw_download(t, h2c_ep: int, cv: int, include_bb: bool = False) -> None:
    """rtw89_fw_download(RTW89_FW_NORMAL): park the CPU, enable it for download, then transfer the
    NORMAL firmware suit (and, when include_bb, the BB-MCU suits) via the firmware module.
    [SRC] fw.c:1984-2047, mac.c:4334."""
    disable_cpu(t)
    fwdl_enable_wcpu(t, 0, True, include_bb)
    firmware.download(t, h2c_ep, cv, include_bb)


def bbmcu_write32(t, addr: int, data: int, phy_idx: int) -> None:
    """rtw89_bbmcu_write32: BB-MCU register write. PHY_1's low regs shift +0x20000 first; every
    write shifts +BBMCU_ADDR_OFFSET. [SRC] phy.h:804-811."""
    if phy_idx and addr < 0x10000:
        addr += 0x20000
    t.write32(addr + RTW89_BBMCU_ADDR_OFFSET, data)


_DMAC_SYS_MASK = (B_BE_DMAC_BB_PHY0_MASK, B_BE_DMAC_BB_PHY1_MASK)       # rtw8922a.c:1797
_BBRST_MASK = (B_BE_FEN_BBPLAT_RSTB, B_BE_FEN_BB1PLAT_RSTB)             # rtw8922a.c:1798
_GLBRST_MASK = (B_BE_FEN_BB_IP_RSTN, B_BE_FEN_BB1_IP_RSTN)             # rtw8922a.c:1799
_MCU_BOOTRDY_MASK = (B_BE_BOOT_RDY0, B_BE_BOOT_RDY1)                    # rtw8922a.c:1800


def bb_preinit(t, phy_idx: int) -> None:
    """rtw8922a_bb_preinit: reset the BB IP/platform, mark the MCU boot-ready, power the BBMCU
    memory, then load the per-PHY BB-MCU init table. [SRC] rtw8922a.c:1802-1818."""
    rdy = 1 if phy_idx == RTW89_PHY_1 else 0
    t.write32_mask(R_BE_DMAC_SYS_CR32B, _DMAC_SYS_MASK[phy_idx], 0x7FF9)
    t.write32_mask(R_BE_FEN_RST_ENABLE, _GLBRST_MASK[phy_idx], 0x0)
    t.write32_mask(R_BE_FEN_RST_ENABLE, _BBRST_MASK[phy_idx], 0x0)
    t.write32_mask(R_BE_FEN_RST_ENABLE, _GLBRST_MASK[phy_idx], 0x1)
    t.write32_mask(R_BE_FEN_RST_ENABLE, _MCU_BOOTRDY_MASK[phy_idx], rdy)
    t.write32_mask(R_BE_MEM_PWR_CTRL, B_BE_MEM_BBMCU0_DS_V1, 0)
    # fsleep(1): 1us settle, sub-resolution in Python and no wire op. [SRC] rtw8922a.c:1816
    for addr, data in BB_MCU_INIT_REG:
        bbmcu_write32(t, addr, data, phy_idx)


def chip_bb_preinit(t) -> None:
    """rtw89_chip_bb_preinit: 8922A runs bb_preinit for PHY_0 and, since dbcc_en is set on BE
    chips, PHY_1. [SRC] core.h:7725-7734, core.c:6992-6993."""
    bb_preinit(t, RTW89_PHY_0)
    bb_preinit(t, RTW89_PHY_1)


def parse_efuse_map(t, cv: int) -> dict:
    """rtw89_parse_efuse_map_be: check autoload, dump the full physical efuse map, then run the
    logical HCI-DIG (USB) and RF block parses. The DAV dump is skipped (dav_phy_efuse_size 0).
    The RF-block parse is pure software; the USB HCI-DIG block reads the MAC address from a
    register. [SRC] efuse_be.c:341-399, rtw8922a.c:854-895.
    TODO: verify, the RF/board logical extraction (rfe_type, xtal, gain, tssi) is not yet done."""
    field_get(B_BE_AUTOLOAD_SUS, t.read16(R_BE_SYS_WL_EFUSE_CTRL))   # efuse->valid
    phy_map = dump_physical_efuse_map(t, cv, 0, PHYSICAL_EFUSE_SIZE)
    addr = bytearray()
    for off in range(0, ETH_ALEN, 2):                # rtw8922a_read_efuse_usb, from R_BE 0x4078
        val = t.read16(R_BE_EFUSE_USB_MACADDR + off)
        addr += bytes((val & 0xFF, (val >> 8) & 0xFF))
    return {"phy_map": phy_map, "mac_addr": bytes(addr)}


def parse_phycap_map(t, cv: int) -> bytes:
    """rtw89_parse_phycap_map_be: dump the phy-capability efuse block. chip->ops->read_phycap
    parses it in software. [SRC] efuse_be.c:402-433, rtw8922a.c:1033.
    TODO: verify, read_phycap (RF path / antenna extraction) is not yet ported."""
    return dump_physical_efuse_map(t, cv, PHYCAP_ADDR, PHYCAP_SIZE)


def read_phycap(t, part_num: int) -> dict:
    """rtw89_mac_read_phycap: query the running firmware for its phy capabilities over the
    register mailbox, bracketed by the efuse-state toggle. [SRC] mac.c:3177-3220."""
    cnv_efuse_state(t, False)
    w0 = field_prep(RTW89_H2CREG_GET_FEATURE_PART_NUM, part_num)
    c2h = firmware.msg_reg(t, RTW89_FWCMD_H2CREG_FUNC_GET_FEATURE, 2, w0)
    cnv_efuse_state(t, True)
    return c2h


def setup_phycap(t) -> None:
    """rtw89_mac_setup_phycap: read the two phy-capability parts from firmware. The C2H payloads
    (tx/rx nss, antenna, QAM) are parsed in software. [SRC] mac.c:3222-3336.
    TODO: verify, the hal extraction (tx_nss/rx_nss/antenna/no_eht/no_mcs_12_13) is not yet done;
    RF/BB init will need it. part1 runs unless the NO_PHYCAP_P1 fw feature is set (it is not here)."""
    read_phycap(t, 0)      # part0, expects C2H id RTW89_FWCMD_C2HREG_FUNC_PHY_CAP
    read_phycap(t, 1)      # part1, expects C2H id RTW89_FWCMD_C2HREG_FUNC_PHY_CAP_PART1


def partial_init(t, h2c_ep: int, cv: int, include_bb: bool = False) -> None:
    """rtw89_mac_partial_init: the pre-firmware DMAC bring-up (dmac_pre_init inlined as
    hci_func_en + dmac_func_pre_en + dle_init + hfc_init), then the firmware download.
    [SRC] mac.c:4307-4338. When include_bb (the interface-up mac_init call) chip_bb_preinit runs
    first; the USB hci mac_pre_init is a no-op on the 8922A. [SRC] usb.c:797-804."""
    ctrl_hci_dma_trx(t, True)
    if include_bb:
        chip_bb_preinit(t)
    hci_func_en(t)
    dmac_func_pre_en(t)
    dle_init(t, "DLFW")
    hfc_init(t, True, False, True)          # reset, en=False, h2c_en=True (download path)
    fwdl_preconfig(t)
    fw_download(t, h2c_ep, cv, include_bb)


def enable_bb_rf(t) -> None:
    """rtw8922a_mac_enable_bb_rf: release the BB platform/IP reset and open both PHYs' DMAC-BB
    path. [SRC] rtw8922a.c:3076, rtw8922a.c enable_bb_rf."""
    t.write8_set(R_BE_FEN_RST_ENABLE, B_BE_FEN_BBPLAT_RSTB | B_BE_FEN_BB_IP_RSTN)
    t.write32(R_BE_DMAC_SYS_CR32B, 0x7FF97FF9)


def sys_init(t) -> None:
    """sys_init_be: dmac/cmac-share func-en are no-ops on the 8922A; cmac_pwr_en(MAC_0) already
    ran in mac_func_en so it is a no-op here, leaving cmac_func_en(MAC_0). chip_func_en is a
    no-op. [SRC] mac_be.c:907-932."""
    cmac_pwr_en(t, RTW89_MAC_0)
    cmac_func_en(t, RTW89_MAC_0)


def sta_sch_init(t) -> None:
    """sta_sch_init_be (8922A, non-D): enable the STA scheduler, wait init-done, set warm-init,
    clear the band trigger enables. [SRC] mac_be.c:971-998."""
    t.write8_set(R_BE_SS_CTRL, B_BE_SS_EN)
    _poll32(t, R_BE_SS_CTRL, lambda v: v & B_BE_SS_INIT_DONE)
    t.write32_set(R_BE_SS_CTRL, B_BE_WARM_INIT)
    t.write32_clr(R_BE_SS_CTRL, B_BE_BAND_TRIG_EN | B_BE_BAND1_TRIG_EN)


def mpdu_proc_init(t) -> None:
    """mpdu_proc_init_be: FCS append, cut-AMSDU control, the TX header-shortcut settings, RX
    header-translation, and the WLAN-CPU forward types. The HDR_SHCUT write32_set re-reads the
    register (a source quirk that overrides the local clear of TX_ADDR_MLD_TO_LIK).
    [SRC] mac_be.c:1000-1033."""
    t.write32_set(R_BE_MPDU_PROC, B_BE_APPEND_FCS)
    t.write32(R_BE_CUT_AMSDU_CTRL, TRXCFG_MPDU_PROC_CUT_CTRL | B_BE_CA_CHK_ADDRCAM_EN)
    val = t.read32(R_BE_HDR_SHCUT_SETTING)
    val = ((val | B_BE_TX_HW_SEQ_EN | B_BE_TX_HW_ACK_POLICY_EN | B_BE_TX_MAC_MPDU_PROC_EN)
           & ~B_BE_TX_ADDR_MLD_TO_LIK & 0xFFFFFFFF)
    t.write32_set(R_BE_HDR_SHCUT_SETTING, val)
    t.write32(R_BE_RX_HDRTRNS, TRXCFG_MPDU_PROC_RX_HDR_CONV | B_BE_HC_ADDR_HIT_EN)
    val = t.read32(R_BE_DISP_FWD_WLAN_0)
    val = field_replace(val, B_BE_FWD_WLAN_CPU_TYPE_0_DATA_MASK, 1)
    val = field_replace(val, B_BE_FWD_WLAN_CPU_TYPE_0_MNG_MASK, 1)
    val = field_replace(val, B_BE_FWD_WLAN_CPU_TYPE_0_CTL_MASK, 1)
    val = field_replace(val, B_BE_FWD_WLAN_CPU_TYPE_1_MASK, 1)
    t.write32(R_BE_DISP_FWD_WLAN_0, val)


def sec_eng_init(t) -> None:
    """sec_eng_init_be: enable the security-engine clocks and the TX-encrypt / RX-decrypt paths,
    then the ICV/MIC append. [SRC] mac_be.c:1035-1059."""
    val = t.read32(R_BE_SEC_ENG_CTRL)
    val |= (B_BE_CLK_EN_CGCMP | B_BE_CLK_EN_WAPI | B_BE_CLK_EN_WEP_TKIP
            | B_BE_SEC_TX_ENC | B_BE_SEC_RX_DEC | B_BE_MC_DEC | B_BE_BC_DEC
            | B_BE_BMC_MGNT_DEC | B_BE_UC_MGNT_DEC | B_BE_SEC_PRE_ENQUE_TX)
    t.write32(R_BE_SEC_ENG_CTRL, val)
    t.write32_set(R_BE_SEC_MPDU_PROC, B_BE_APPEND_ICV | B_BE_APPEND_MIC)


def txpktctrl_init(t) -> None:
    """txpktctrl_init_be: program the MPDU-info table. pktid is the DBCC ple free-page count
    (dle_info.ple_free_pg); the band-1 base offset is the fixed MPDU_INFO_B1_OFST since the 8922A
    has no dle_input (that is 8922D+). [SRC] mac_be.c:1061-1091."""
    pktid = PLE_SIZE7_LNK_PGE_NUM           # dle_info.ple_free_pg for the DBCC config
    val = t.read32(R_BE_TXPKTCTL_MPDUINFO_CFG)
    val = field_replace(val, B_BE_MPDUINFO_PKTID_MASK, pktid)
    val = field_replace(val, B_BE_MPDUINFO_B1_BADDR_MASK, MPDU_INFO_B1_OFST)
    val |= B_BE_MPDUINFO_FEN
    t.write32(R_BE_TXPKTCTL_MPDUINFO_CFG, val)


def mlo_init(t) -> None:
    """mlo_init_be: pulse the MLO table reinit, wait init-done, then enable HW change-link and the
    MACID acquire check. reg is R_BE_SS_CTRL on the 8922A. [SRC] mac_be.c:1093-1129."""
    val = t.read32(R_BE_MLO_INIT_CTL)
    t.write32(R_BE_MLO_INIT_CTL, val | B_BE_MLO_TABLE_REINIT)
    t.write32(R_BE_MLO_INIT_CTL, val & ~B_BE_MLO_TABLE_REINIT & 0xFFFFFFFF)
    _poll32(t, R_BE_MLO_INIT_CTL, lambda v: v & B_BE_MLO_TABLE_INIT_DONE)
    t.write32_set(R_BE_SS_CTRL, B_BE_MLO_HW_CHGLINK_EN)
    t.write32_set(R_BE_CMAC_SHARE_ACQCHK_CFG_0, B_BE_R_MACID_ACQ_CHK_EN)


def dmac_init(t) -> None:
    """dmac_init_be(0): the DMAC-side operating init. dle_init (DBCC qta), hfc_init (operating),
    sta_sch_init, mpdu_proc_init, sec_eng_init, txpktctrl_init, mlo_init. preload_init is a no-op
    on USB (not qta_poh). [SRC] mac_be.c:1131-1184, mac.c:preload_init."""
    dle_init(t, "DBCC")
    hfc_init(t, True, True, True)           # reset, en=True, h2c_en=True (operating path)
    sta_sch_init(t)
    mpdu_proc_init(t)
    sec_eng_init(t)
    txpktctrl_init(t)
    mlo_init(t)


def scheduler_init(t, mac_idx: int) -> None:
    """scheduler_init_be (8922A, non-D): the HE contention / SIFS / TB NAV+CCA check masks, clear
    the no-grant-WL CCA enable, then the beacon-queue CW/AIFS. is_qta_poh is false on USB so the
    prebackoff config is skipped. [SRC] mac_be.c:1186-1245."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    t.write32(R_BE_HE_CTN_CHK_CCA_NAV + off,
              B_BE_HE_CTN_CHK_CCA_P20 | B_BE_HE_CTN_CHK_EDCCA_P20 | B_BE_HE_CTN_CHK_CCA_BITMAP
              | B_BE_HE_CTN_CHK_EDCCA_BITMAP | B_BE_HE_CTN_CHK_NO_GNT_WL | B_BE_HE_CTN_CHK_BASIC_NAV
              | B_BE_HE_CTN_CHK_INTRA_NAV | B_BE_HE_CTN_CHK_TX_NAV)
    t.write32(R_BE_HE_SIFS_CHK_CCA_NAV + off,
              B_BE_HE_SIFS_CHK_EDCCA_P20 | B_BE_HE_SIFS_CHK_EDCCA_BITMAP | B_BE_HE_SIFS_CHK_NO_GNT_WL)
    t.write32(R_BE_TB_CHK_CCA_NAV + off,
              B_BE_TB_CHK_EDCCA_BITMAP | B_BE_TB_CHK_NO_GNT_WL | B_BE_TB_CHK_BASIC_NAV)
    t.write32_clr(R_BE_CCA_CFG_0 + off, B_BE_NO_GNT_WL_EN)
    t.write32_mask(R_BE_EDCA_BCNQ_PARAM + off, B_BE_BCNQ_CW_MASK, 0x32)
    t.write32_mask(R_BE_EDCA_BCNQ_PARAM + off, B_BE_BCNQ_AIFS_MASK, BCN_IFS_25US)


def addr_cam_init(t, mac_idx: int) -> None:
    """addr_cam_init_be: set the address-CAM search range and enable, request a clear on MAC_0,
    then poll the clear done. [SRC] mac_be.c:1247-1276."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read32(R_BE_ADDR_CAM_CTRL + off)
    val = field_replace(val, B_BE_ADDR_CAM_RANGE_MASK, ADDR_CAM_SERCH_RANGE) | B_BE_ADDR_CAM_EN
    if mac_idx == RTW89_MAC_0:
        val |= B_BE_ADDR_CAM_CLR
    t.write32(R_BE_ADDR_CAM_CTRL + off, val)
    for _ in range(PWR_POLL_ATTEMPTS):
        if not (t.read16(R_BE_ADDR_CAM_CTRL + off) & B_BE_ADDR_CAM_CLR):
            break


def _typ_fltr_opt(t, reg: int, mac_idx: int) -> None:
    """rtw89_mac_typ_fltr_opt_be(FWD_TO_HOST): accept the frame type into the host. [SRC] mac_be.c:1275."""
    t.write32(reg + mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET, RX_FLTR_FRAME_ACCEPT_BE)


def rx_fltr_init(t, mac_idx: int) -> None:
    """rx_fltr_init_be: accept mgnt/ctrl/data to host, set the RX filter options (sniffer mode plus
    the CAM/BC/MC address matches and a UID filter of 15), and the PLCP-header CRC checks. The
    sniffer-mode bit is what puts RX into monitor. [SRC] mac_be.c:1315-1336."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    _typ_fltr_opt(t, R_BE_MGNT_FLTR, mac_idx)
    _typ_fltr_opt(t, R_BE_CTRL_FLTR, mac_idx)
    _typ_fltr_opt(t, R_BE_DATA_FLTR, mac_idx)
    t.write32(R_BE_RX_FLTR_OPT + off,
              B_BE_A_BC_CAM_MATCH | B_BE_A_UC_CAM_MATCH | B_BE_A_MC | B_BE_A_BC
              | B_BE_A_A1_MATCH | B_BE_SNIFFER_MODE | field_prep(B_BE_UID_FILTER_MASK, 15))
    t.write16(R_BE_PLCP_HDR_FLTR + off,
              B_BE_HE_SIGB_CRC_CHK | B_BE_VHT_MU_SIGB_CRC_CHK | B_BE_VHT_SU_SIGB_CRC_CHK
              | B_BE_SIGA_CRC_CHK | B_BE_LSIG_PARITY_CHK_EN | B_BE_CCK_SIG_CHK | B_BE_CCK_CRC_CHK)


def nav_ctrl_init(t, mac_idx: int) -> None:
    """nav_ctrl_init_be: NAV upper-bound + TF-up enable (PLCP-up disabled), clear the BMC NAV
    protect, force the MBA duration. cca_ctrl_init_be is a no-op. [SRC] mac_be.c nav_ctrl_init_be."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read32(R_BE_WMAC_NAV_CTL + off)
    val = (val & ~B_BE_WMAC_PLCP_UP_NAV_EN & 0xFFFFFFFF) | B_BE_WMAC_TF_UP_NAV_EN | B_BE_WMAC_NAV_UPPER_EN
    val = field_replace(val, B_BE_WMAC_NAV_UPPER_MASK, NAV_25MS)
    t.write32(R_BE_WMAC_NAV_CTL + off, val)
    t.write32_clr(R_BE_SPECIAL_TX_SETTING + off, B_BE_BMC_NAV_PROTECT)
    t.write32_set(R_BE_TRXPTCL_RESP_0 + off, B_BE_WMAC_MBA_DUR_FORCE)


def spatial_reuse_init(t, mac_idx: int) -> None:
    """spatial_reuse_init_be: disable spatial-reuse RX, enable the PLCP BSSID source.
    [SRC] mac_be.c:1376-1396."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    t.write8_clr(R_BE_RX_SR_CTRL + off, B_BE_SR_EN | B_BE_SR_CTRL_PLCP_EN)
    t.write8_set(R_BE_BSSID_SRC_CTRL + off, B_BE_PLCP_SRC_EN)


def tmac_init(t, mac_idx: int) -> None:
    """tmac_init_be (8922A): clear the QoS-null MU-EDCA update, set the EHT/HE PPDU 4x/2x-LTF
    zero-LD micro-timers. [SRC] mac_be.c:1398-1423."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    t.write32_clr(R_BE_TB_PPDU_CTRL + off, B_BE_QOSNULL_UPD_MUEDCA_EN)
    t.write32_mask(R_BE_WMTX_TCR_BE_4 + off, B_BE_EHT_HE_PPDU_4XLTF_ZLD_USTIMER_MASK, 0x12)
    t.write32_mask(R_BE_WMTX_TCR_BE_4 + off, B_BE_EHT_HE_PPDU_2XLTF_ZLD_USTIMER_MASK, 0xe)


def trxptcl_init(t, mac_idx: int, cv: int) -> None:
    """trxptcl_init_be (8922A): MAC-loopback PLCP delay (loopback off), PHYINTF enable, the
    response SIFS values, clear the ACK/BA EDCCA checks, RX-trigger FCS check, mask down the
    response and RRSR rate tables, and apply the rrsr ref-rate (REF_RATE_SEL cleared). The 8922D
    PLCP-CRC branch is skipped; the CAV-only RSC=1 write runs only on the A-cut. [SRC] mac_be.c:1425-1502."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read32(R_BE_MAC_LOOPBACK + off)
    val = field_replace(val, B_BE_MACLBK_PLCP_DLY_MASK, S_BE_MACLBK_PLCP_DLY_DEF) & ~B_BE_MACLBK_EN
    t.write32(R_BE_MAC_LOOPBACK + off, val & 0xFFFFFFFF)
    t.write32_set(R_BE_CMAC_FUNC_EN + off, B_BE_PHYINTF_EN)

    val = t.read32(R_BE_TRXPTCL_RESP_0 + off)
    val = field_replace(val, B_BE_WMAC_SPEC_SIFS_CCK_MASK, WMAC_SPEC_SIFS_CCK)
    val = field_replace(val, B_BE_WMAC_SPEC_SIFS_OFDM_MASK, WMAC_SPEC_SIFS_OFDM_1115E)
    t.write32(R_BE_TRXPTCL_RESP_0 + off, val)

    t.write32_clr(R_BE_WMAC_ACK_BA_RESP_LEGACY + off, B_BE_ACK_BA_RESP_LEGACY_CHK_EDCCA)
    t.write32_clr(R_BE_WMAC_ACK_BA_RESP_HE + off, B_BE_ACK_BA_RESP_HE_CHK_EDCCA)
    t.write32_clr(R_BE_WMAC_ACK_BA_RESP_EHT_LEG_PUNC + off, B_BE_ACK_BA_EHT_LEG_PUNC_CHK_EDCCA)
    t.write32_set(R_BE_RXTRIG_TEST_USER_2 + off, B_BE_RXTRIG_FCSCHK_EN)

    val = t.read32(R_BE_TRXPTCL_RESP_1 + off)
    val &= (B_BE_FTM_RRSR_RATE_EN_MASK | B_BE_WMAC_RESP_DOPPLEB_BE_EN | B_BE_WMAC_RESP_DCM_EN
            | B_BE_WMAC_RESP_REF_RATE_MASK)
    t.write32(R_BE_TRXPTCL_RESP_1 + off, val)
    t.write32_mask(R_BE_TRXPTCL_RESP_1 + off, B_BE_WMAC_RESP_REF_RATE_SEL, 0)   # rrsr.ref_rate

    val = t.read32(R_BE_PTCL_RRSR1 + off)
    val &= (B_BE_RRSR_RATE_EN_MASK | B_BE_RRSR_CCK_MASK | B_BE_RSC_MASK)
    t.write32(R_BE_PTCL_RRSR1 + off, val)

    val = t.read32(R_BE_PTCL_RRSR0 + off)
    val &= (B_BE_RRSR_OFDM_MASK | B_BE_RRSR_HT_MASK | B_BE_RRSR_VHT_MASK | B_BE_RRSR_HE_MASK)
    t.write32(R_BE_PTCL_RRSR0 + off, val)

    if cv == CHIP_CAV:
        t.write32_mask(R_BE_PTCL_RRSR1 + off, B_BE_RSC_MASK, 1)


def _rst_bacam(t) -> None:
    """rst_bacam_be: reset the whole response-BA CAM, poll reset-done. [SRC] mac_be.c:1504-1519."""
    t.write32_mask(R_BE_RESPBA_CAM_CTRL, B_BE_BACAM_RST_MASK, S_BE_BACAM_RST_ALL)
    for _ in range(PWR_POLL_ATTEMPTS):
        if field_get(B_BE_BACAM_RST_MASK, t.read32(R_BE_RESPBA_CAM_CTRL)) == S_BE_BACAM_RST_DONE:
            break


def rmac_init(t, mac_idx: int) -> None:
    """rmac_init_be (8922A): reset the BA-CAM (MAC0 only), set the RX dead-lock protect timers, the
    RX MPDU max length (derived from the DBCC c0 RX quota and PLE page size), disable the VHT-SU
    SIGB CRC check, and set busy-checksum + the SU PSDU-length source. [SRC] mac_be.c:1525-1587."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    if mac_idx == RTW89_MAC_0:
        _rst_bacam(t)
    val = t.read16(R_BE_DLK_PROTECT_CTL + off)
    val = field_replace(val, B_BE_RX_DLK_DATA_TIME_MASK, TRXCFG_RMAC_DATA_TO)
    val = field_replace(val, B_BE_RX_DLK_CCA_TIME_MASK, TRXCFG_RMAC_CCA_TO) | B_BE_RX_DLK_RST_EN
    t.write16(R_BE_DLK_PROTECT_CTL + off, val & 0xFFFF)

    c0_rx_qta = PLE_QT14_V1[6]                        # dle_info.c0_rx_qta = ple_min_qt.cma0_dma
    rx_max_pg = min(c0_rx_qta, PLD_RLS_MAX_PG)
    rx_max_len = min(rx_max_pg * RTW89_PLE_PG_256, RX_SPEC_MAX_LEN) // RX_MAX_LEN_UNIT
    t.write32_mask(R_BE_RX_FLTR_OPT + off, B_BE_RX_MPDU_MAX_LEN_MASK, rx_max_len)
    t.write8_clr(R_BE_PLCP_HDR_FLTR + off, B_BE_VHT_SU_SIGB_CRC_CHK)
    t.write16_set(R_BE_RCR + off, B_BE_BUSY_CHKSN)
    t.write16_set(R_BE_RX_PLCP_EXT_OPTION_1 + off, B_BE_PLCP_SU_PSDU_LEN_SRC)


def resp_pktctl_init(t, mac_idx: int) -> None:
    """resp_pktctl_init_be: the CSI-response reserved page from the B0/B1 CSI reserved quota.
    pktid = DBCC ple free-page + reserved mpdu-info-tbl (+ b0_csi for band 1); the page count is
    the band's csi pages + 1. [SRC] mac_be.c:1589-1616, mac.c:1946-1954."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    if mac_idx == RTW89_MAC_1:                        # DLE_RSVD_QT_B1_CSI
        pktid = PLE_SIZE7_LNK_PGE_NUM + PLE_RSVD_QT2[0] + PLE_RSVD_QT2[1]
        pg_num = PLE_RSVD_QT2[2]                      # b1_csi
    else:                                             # DLE_RSVD_QT_B0_CSI
        pktid = PLE_SIZE7_LNK_PGE_NUM + PLE_RSVD_QT2[0]
        pg_num = PLE_RSVD_QT2[1]                      # b0_csi
    t.write32_mask(R_BE_RESP_CSI_RESERVED_PAGE + off, B_BE_CSI_RESERVED_START_PAGE_MASK, pktid)
    t.write32_mask(R_BE_RESP_CSI_RESERVED_PAGE + off, B_BE_CSI_RESERVED_PAGE_NUM_MASK, pg_num + 1)


_TXSB_20M = (S_BE_TXSB_20M_8, S_BE_TXSB_20M_2)      # per mac_idx. mac_be.c:1629-1639
_TXSB_40M = (S_BE_TXSB_40M_4, S_BE_TXSB_40M_1)
_TXSB_80M = (S_BE_TXSB_80M_2, S_BE_TXSB_80M_0)
_TXSB_160M = (S_BE_TXSB_160M_1, S_BE_TXSB_160M_0)


def cmac_com_init(t, mac_idx: int) -> None:
    """cmac_com_init_be: the per-band TX sub-band values (band-1 register is the +0x4000 alias).
    [SRC] mac_be.c:1618-1644."""
    reg = R_BE_TX_SUB_BAND_VALUE + mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read32(reg)
    val = field_replace(val, B_BE_TXSB_20M_MASK, _TXSB_20M[mac_idx])
    val = field_replace(val, B_BE_TXSB_40M_MASK, _TXSB_40M[mac_idx])
    val = field_replace(val, B_BE_TXSB_80M_MASK, _TXSB_80M[mac_idx])
    val = field_replace(val, B_BE_TXSB_160M_MASK, _TXSB_160M[mac_idx])
    t.write32(reg, val)


def ptcl_init(t, mac_idx: int) -> None:
    """ptcl_init_be (8922A, USB): CMAC TX modes on, spatial-trigger SS off, AMPDU max time. The
    qta_poh SIFS/FSM block and the PCIE-only preload block are skipped on USB. [SRC] mac_be.c:1646-1732."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read8(R_BE_PTCL_COMMON_SETTING_0 + off)
    val |= B_BE_CMAC_TX_MODE_0 | B_BE_CMAC_TX_MODE_1
    val &= ~(B_BE_PTCL_TRIGGER_SS_EN_0 | B_BE_PTCL_TRIGGER_SS_EN_1 | B_BE_PTCL_TRIGGER_SS_EN_UL) & 0xFF
    t.write8(R_BE_PTCL_COMMON_SETTING_0 + off, val)
    t.write32_mask(R_BE_AMPDU_AGG_LIMIT + off, B_BE_AMPDU_MAX_TIME_MASK, AMPDU_MAX_TIME)


def cmac_dma_init(t, mac_idx: int) -> None:
    """cmac_dma_init_be: route the TX-report and F2P-cmd-report RX queues to the WLAN-CPU RX
    channel 2. [SRC] mac_be.c:1734-1753."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    val = t.read32(R_BE_RX_CTRL_1 + off)
    val = field_replace(val, B_BE_RXDMA_TXRPT_QUEUE_ID_SW_MASK, WLCPU_RXCH2_QID)
    val = field_replace(val, B_BE_RXDMA_F2PCMDRPT_QUEUE_ID_SW_MASK, WLCPU_RXCH2_QID)
    t.write32(R_BE_RX_CTRL_1 + off, val)


def cmac_init(t, mac_idx: int, cv: int) -> None:
    """cmac_init_be: the CMAC-side operating init (scheduler, addr-cam, rx-filter, cca/nav,
    spatial-reuse, tmac, trxptcl, rmac, resp-pktctl, com, ptcl, ...). cca_ctrl_init_be is a no-op.
    [SRC] mac_be.c:1756+."""
    scheduler_init(t, mac_idx)
    addr_cam_init(t, mac_idx)
    rx_fltr_init(t, mac_idx)
    nav_ctrl_init(t, mac_idx)
    spatial_reuse_init(t, mac_idx)
    tmac_init(t, mac_idx)
    trxptcl_init(t, mac_idx, cv)
    rmac_init(t, mac_idx)
    resp_pktctl_init(t, mac_idx)
    cmac_com_init(t, mac_idx)
    ptcl_init(t, mac_idx)
    cmac_dma_init(t, mac_idx)


def tx_idle_poll_band(t, mac_idx: int) -> None:
    """tx_idle_poll_band_be: poll the band's PTCL TX-contention until not busy. [SRC] mac_be.c:1846-1862."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    for _ in range(PWR_POLL_ATTEMPTS):
        if not (t.read8(R_BE_PTCL_TX_CTN_SEL + off) & B_BE_PTCL_BUSY):
            break


def _dle_upd_qta_aval_page_ple(t, quota_id: int) -> None:
    """dle_upd_qta_aval_page_be(PLE): request a PLE available-page recompute for the quota id.
    [SRC] mac_be.c dle_upd_qta_aval_page_be."""
    t.write32_mask(R_BE_PLE_BUFMGN_CTL, B_BE_PLE_AVAL_UPD_QTAID_MASK, quota_id)
    t.write32_set(R_BE_PLE_BUFMGN_CTL, B_BE_PLE_AVAL_UPD_REQ)
    _poll32(t, R_BE_PLE_BUFMGN_CTL, lambda v: not (v & B_BE_PLE_AVAL_UPD_REQ))


def dle_quota_change(t) -> None:
    """rtw89_mac_dle_quota_change(DBCC, band1): re-apply the DBCC quotas, then recompute the PLE
    available pages for the B0-TX and CMAC0-RX quotas. [SRC] mac.c dle_quota_change, mac_be.c."""
    cfg = _DLE_CFG["DBCC"]
    dle_quota_cfg(t, cfg["wde_qt"], cfg["ple_min_qt"], cfg["ple_max_qt"], cfg["ext_wcpu"])
    _dle_upd_qta_aval_page_ple(t, PLE_QTAID_B0_TXPL)
    _dle_upd_qta_aval_page_ple(t, PLE_QTAID_CMAC0_RX)


def dbcc_bb_ctrl(t) -> None:
    """dbcc_bb_ctrl_be(bb1_en=True): release the band-1 BB platform/IP reset. [SRC] mac_be.c."""
    t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_FEN_BB1PLAT_RSTB | B_BE_FEN_BB1_IP_RSTN)


_PRELD_CFG0 = (R_BE_TXPKTCTL_B0_PRELD_CFG0, R_BE_TXPKTCTL_B1_PRELD_CFG0)
_PRELD_CFG1 = (R_BE_TXPKTCTL_B0_PRELD_CFG1, R_BE_TXPKTCTL_B1_PRELD_CFG1)
_PRELD_ENT_NUM = (PRELD_B0_ENT_NUM, PRELD_B1_ENT_NUM)
_PRELD_ACQ_ENT_NUM = (PRELD_B0_ACQ_ENT_NUM_8922A, PRELD_B1_ACQ_ENT_NUM_8922A)


def preload_init(t, mac_idx: int) -> None:
    """preload_init_be (chip op, always runs for the 8922A). The is_qta_poh-gated mac.c wrapper
    no-ops on USB, so this is only reached for MAC_1 in band1_enable. Program the per-band TX
    preload next-window / entry counts / max size. [SRC] mac_be.c:2013-2058."""
    max_preld_size = _PRELD_ENT_NUM[mac_idx] * PRELD_AMSDU_SIZE
    reg1 = _PRELD_CFG1[mac_idx]
    val = t.read32(reg1)
    val = field_replace(val, B_BE_B0_PRELD_NXT_TXENDWIN_MASK, PRELD_NEXT_WND)
    val = field_replace(val, B_BE_B0_PRELD_NXT_RSVMINSZ_MASK, PRELD_NEXT_MIN_SIZE)
    t.write32(reg1, val)
    reg0 = _PRELD_CFG0[mac_idx]
    val = t.read32(reg0)
    val = field_replace(val, B_BE_B0_PRELD_CAM_G0ENTNUM_MASK, _PRELD_ACQ_ENT_NUM[mac_idx])
    val = field_replace(val, B_BE_B0_PRELD_CAM_G1ENTNUM_MASK, PRELD_MISCQ_ENT_NUM_8922A)
    val = field_replace(val, B_BE_B0_PRELD_USEMAXSZ_MASK, max_preld_size) | B_BE_B0_PRELD_FEN
    t.write32(reg0, val)


def enable_imr(t, mac_idx: int, table) -> None:
    """enable_imr_be: apply an IMR (addr, clr, set) table at the band offset. The 8922D-only
    NO_RX_ERR write is skipped. [SRC] mac_be.c:2085-2115, rtw8922a.c:269-323."""
    off = mac_idx * RTW89_MAC_BE_BAND_REG_OFFSET
    for addr, clr, st in table:
        val = t.read32(addr + off)
        t.write32(addr + off, (val & ~clr & 0xFFFFFFFF) | st)


def band1_enable(t, cv: int) -> None:
    """band1_enable_be: TX-idle poll, DBCC quota change, band-1 TX preload, then CMAC1 func-en and
    a second full cmac_init at the band-1 offset, the band-1 BB enable, then the CMAC1 IMR.
    cmac_pwr_en(MAC1) already ran in mac_func_en. [SRC] mac_be.c band1_enable_be."""
    tx_idle_poll_band(t, RTW89_MAC_0)
    dle_quota_change(t)
    preload_init(t, RTW89_MAC_1)
    cmac_pwr_en(t, RTW89_MAC_1)          # no-op: CMAC1 already powered in mac_func_en
    cmac_func_en(t, RTW89_MAC_1)
    cmac_init(t, RTW89_MAC_1, cv)
    dbcc_bb_ctrl(t)
    enable_imr(t, RTW89_MAC_1, IMR_CMAC_REGS)


def dbcc_enable(t, cv: int, h2c_ep: int) -> None:
    """dbcc_enable_be(True): bring up band 1, then notify the running fw (FW_RDY is set after the
    firmware download). [SRC] mac_be.c dbcc_enable_be."""
    band1_enable(t, cv)
    firmware.h2c_notify_dbcc(t, h2c_ep, True)


def err_imr_ctrl(t) -> None:
    """err_imr_ctrl_be(en=True): enable the DMAC (minus NOTX) and CMAC0/CMAC1 error IMRs. The C1
    write runs because dbcc_en is set. [SRC] mac_be.c err_imr_ctrl_be."""
    t.write32(R_BE_DMAC_ERR_IMR, DMAC_ERR_IMR_EN & ~B_BE_DMAC_NOTX_ERR_INT_EN & 0xFFFFFFFF)
    t.write32(R_BE_CMAC_ERR_IMR, CMAC0_ERR_IMR_EN)
    t.write32(R_BE_CMAC_ERR_IMR_C1, CMAC1_ERR_IMR_EN)


def set_host_rpr(t) -> None:
    """set_host_rpr_be (8922A, non-POH STF mode): the WD-release report mode/queue and the release
    report filter map (TXOK/RTYLMT/LIFTIM/MACID), aggregation, and timeout. [SRC] mac_be.c set_host_rpr_be."""
    fltr = (S_BE_WDRLS_FLTR_TXOK | S_BE_WDRLS_FLTR_RTYLMT | S_BE_WDRLS_FLTR_LIFTIM
            | S_BE_WDRLS_FLTR_MACID)
    t.write32_mask(R_BE_WDRLS_CFG, B_BE_WDRLS_MODE_MASK, RTW89_RPR_MODE_STF)
    t.write32_mask(R_BE_RLSRPT0_CFG0, B_BE_RLSRPT0_QID_MASK, WDRLS_DEST_QID_STF)
    val = t.read32(R_BE_RLSRPT0_CFG1)
    val = field_replace(val, B_BE_RLSRPT0_FLTR_MAP_MASK, fltr)
    val = field_replace(val, B_BE_RLSRPT0_AGGNUM_MASK, 30)
    val = field_replace(val, B_BE_RLSRPT0_TO_MASK, 255)
    t.write32(R_BE_RLSRPT0_CFG1, val)


def trx_init(t, cv: int, h2c_ep: int) -> None:
    """trx_init_be: dmac_init then cmac_init, the dbcc enable (qta is DBCC), the DMAC/CMAC IMR
    enables, err-imr-ctrl, host-rpr, and the 8922A rsp-chk-sig clear. [SRC] mac_be.c:2302-2352."""
    dmac_init(t)
    cmac_init(t, RTW89_MAC_0, cv)
    dbcc_enable(t, cv, h2c_ep)
    enable_imr(t, RTW89_MAC_0, IMR_DMAC_REGS)
    enable_imr(t, RTW89_MAC_0, IMR_CMAC_REGS)
    err_imr_ctrl(t)
    set_host_rpr(t)
    t.write32_clr(R_BE_RSP_CHK_SIG, B_BE_RSP_STATIC_RTS_CHK_SERV_BW_EN)   # chip_id == RTL8922A


def feat_init(t, h2c_ep: int) -> None:
    """rtw89_mac_feat_init: bacam_ver is V1 on the 8922A, so seed the BA-CAM users for MAC_0 and
    MAC_1 (1 RU-support STA each). [SRC] mac.c rtw89_mac_feat_init."""
    firmware.h2c_init_ba_cam_users(t, h2c_ep, 1, 0, RTW89_MAC_0)
    firmware.h2c_init_ba_cam_users(t, h2c_ep, 1, 1 * BACAM_1024BMP_OCC_ENTRY, RTW89_MAC_1)


def mac_post_init(t) -> None:
    """rtw89_usb_ops_mac_post_init -> rx_agg_cfg_v3 (8922A): enable RX aggregation (num-th 255,
    32us time, 20 * 1K buffer). The non-8922A SSPHY/endpoint config is skipped. [SRC] usb.c:881."""
    rxagg0 = (B_BE_RXAGG_0_EN | field_prep(B_BE_RXAGG_0_NUM_TH, 255)
              | field_prep(B_BE_RXAGG_0_TIME_32US_TH, 32) | field_prep(B_BE_RXAGG_0_BUF_SZ_1K, 20))
    t.write32(R_BE_RXAGG_0_V1, rxagg0)
    t.write32(R_BE_RXAGG_1_V1, 0x1F)


def mac_init(t, h2c_ep: int, cv: int) -> None:
    """rtw89_mac_init: the interface-up MAC bring-up. partial_init(include_bb=True) (BB preinit +
    BB-MCU firmware re-download), enable_bb_rf, sys_init, trx_init, feat_init, the USB mac_post_init
    (RX aggregation), then the fw offload config H2C. The early-h2c list is empty. [SRC] mac.c:4359-4400."""
    partial_init(t, h2c_ep, cv, include_bb=True)
    enable_bb_rf(t)
    sys_init(t)
    trx_init(t, cv, h2c_ep)
    feat_init(t, h2c_ep)
    mac_post_init(t)
    firmware.h2c_set_ofld_cfg(t, h2c_ep)
