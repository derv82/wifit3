"""Hardware-free MAC-area regression for the rtl8812au_dkms port.

Every expected address AND value below is derived from morrownr's 8812au C oracle under
``driver_captures/captures_8812au/driver-source/`` (cited by file:line above each assertion).
A failing test therefore means the Python port diverged from the C. Nothing here is copied
from any Python source file.

Build config pinned from ``include/autoconf.h`` (this driver's compile):
  CONFIG_XMIT_ACK             defined  (autoconf.h:56)
  CONFIG_80211AC_VHT          defined  -> CONFIG_BEAMFORMING (autoconf.h:62,70)
                                       -> CONFIG_BEAMFORMER_FW_NDPA (hal_ic_cfg.h:73-74)
  CONFIG_SUPPORT_USB_INT      NOT set  (autoconf.h:75, commented)
  CONFIG_USB_TX_AGGREGATION   defined  (autoconf.h:197)
  CONFIG_USB_RX_AGGREGATION   defined  (autoconf.h:198)
  CONFIG_WOWLAN               NOT set  -> WOWLAN_PAGE_NUM_8812 = 0
  CONFIG_FW_C2H_DEBUG         NOT set  -> RX_DMA_RESERVED_SIZE_8812 = 0
  DBG_FW_DEBUG_MSG_PKT        NOT set  -> FW_DBG_MSG_PKT_PAGE_NUM_8812 = 0
  RTL8812A_RX_PACKET_INCLUDE_CRC = 0   (autoconf.h:244) -> no ACRC32
  CONFIG_RX_PACKET_APPEND_FCS defined  (autoconf.h:246) -> RCR_APPFCS
  ENABLE_USB_DROP_INCORRECT_OUT defined (autoconf.h:252)
  CONFIG_ADHOC_WORKAROUND_SETTING = 1  (autoconf.h:254) -> BCN_MAX_ERR = 0xFF
  MP_DRIVER = 0                        (autoconf.h:266) -> non-MP turn-on tail
  CONFIG_PREALLOC_RX_SKB_BUFFER NOT set -> rxagg_usb size=0x5 timeout=0x20
"""
import pytest

from wifit3.chips.rtl8812au_dkms.mac import (
    hal_init_misc_post,
    hal_init_misc_pre,
    mac_init_misc,
    phy_mac_config,
)


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def read8(self, a):
        self.ops.append(("R8", a))
        return self.reads.get(a, 0)

    def read16(self, a):
        self.ops.append(("R16", a))
        return self.reads.get(a, 0)

    def read32(self, a):
        self.ops.append(("R32", a))
        return self.reads.get(a, 0)

    def write8(self, a, v):
        self.ops.append(("W8", a, v))

    def write16(self, a, v):
        self.ops.append(("W16", a, v))

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def _writes(rec):
    return [o for o in rec.ops if o[0] in ("W8", "W16", "W32")]


def _index(rec, op):
    return rec.ops.index(op)


# USB2 (high-speed) + 512B burst: read8(0xFF) bit7 set selects USB2; read8(0xFE17) upper-nibble
# low-2-bits == 0 selects the 512B sub-branch (usb_halinit.c:227-237).
DEFAULT_MISC_READS = {0x00FF: 0x80, 0xFE17: 0x00}


def _misc(reads_override=None):
    reads = dict(DEFAULT_MISC_READS)
    if reads_override:
        reads.update(reads_override)
    rec = Rec(reads=reads)
    mac_init_misc(rec)
    return rec


# =====================================================================================
# phy_mac_config  --  array_mp_8812a_mac_reg walk (byte writes via odm_config_mac_8812a)
# =====================================================================================

def test_table_row_0x010_first_unconditional():  # branch 3
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:119  ->  0x010, 0x0000000C  (first data row, before the IF block)
    assert ("W8", 0x010, 0x0C) in rec.ops


def test_table_row_0x011_usb_if_branch_writes_0x66():  # branch 1 + suspicion flag (phy_cond IF)
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:121  ->  0x011, 0x00000066 under IF cond 0x80000200 (support_interface==USB)
    assert ("W8", 0x011, 0x66) in rec.ops


def test_table_row_0x011_else_branch_suppressed():  # branch 2
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:123  ->  0x011, 0x0000005A under ELSE (0xA0000000); MUST NOT fire once
    # the USB IF matched (is_skipped=true). Absence-of-write assertion.
    assert ("W8", 0x011, 0x5A) not in rec.ops
    # exactly one write to 0x011 total (the IF value)
    assert [o for o in rec.ops if o[0] == "W8" and o[1] == 0x011] == [("W8", 0x011, 0x66)]


def test_table_row_0x025_after_endif():  # branch 3
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:125  ->  0x025, 0x0000000F  (first unconditional row after ENDIF 0xB0000000)
    assert ("W8", 0x025, 0x0F) in rec.ops


def test_table_spotcheck_mid_rows():  # branch 3 / table_spotcheck
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:146  ->  0x440, 0x0000005D
    assert ("W8", 0x440, 0x5D) in rec.ops
    # halhwimg8812a_mac.c:170  ->  0x4CC, 0x000000FF
    assert ("W8", 0x4CC, 0xFF) in rec.ops
    # halhwimg8812a_mac.c:191  ->  0x516, 0x0000000A
    assert ("W8", 0x516, 0x0A) in rec.ops


def test_table_last_row_0x718_is_final_write():  # branch 3 / table_spotcheck
    rec = Rec()
    phy_mac_config(rec)
    # halhwimg8812a_mac.c:229  ->  0x718, 0x00000040  (LAST data row of array_mp_8812a_mac_reg)
    assert ("W8", 0x718, 0x40) in rec.ops
    assert _writes(rec)[-1] == ("W8", 0x718, 0x40)


def test_table_writes_are_all_byte_writes():  # branch 3 (odm_write_1byte)
    rec = Rec()
    phy_mac_config(rec)
    # phydm_regconfig8812a.c:61-67 odm_config_mac_8812a -> odm_write_1byte : every MAC row is a write8
    assert all(o[0] == "W8" for o in _writes(rec))


# =====================================================================================
# mac_init_misc  --  MISC01+MISC02 body, ending at REG_CR MACTXEN|MACRXEN
# =====================================================================================

def test_hardware_type_8812_variant_calls():  # branch 4
    rec = _misc()
    # usb_halinit.c:1529-1530 _InitTransferPageSize_8812AUsb is 8812-only (8821 skips the call);
    # value = _PSTX(PBP_512) = 0x3<<4 = 0x30 written to REG_PBP=0x104 (hal_com_reg.h:118,1371,1376)
    assert ("W8", 0x104, 0x30) in rec.ops
    # usb_halinit.c:587-588 8812 RX-DMA boundary = RX_DMA_BOUNDARY_8812 = 0x3E7F (not the 8821 value)
    assert ("W16", 0x116, 0x3E7F) in rec.ops


def test_rqpn_reserved_page_npq_before_rqpn():  # branch 5 (+ suspicion flag: PUBQ=0xD6 not 0xD8)
    rec = _misc()
    # usb_halinit.c:510-511  value8 = _NPQ(numNQ=0) = 0x00 -> REG_RQPN_NPQ=0x214 (hal_com_reg.h:176,1437)
    npq = ("W8", 0x214, 0x00)
    # usb_halinit.c:514-515  value32 = _HPQ(0x10)|_LPQ(0x10)|_PUBQ(0xD6)|LD_RQPN
    #   NORMAL_PAGE_NUM_HPQ/LPQ_8812 = 0x10, NORMAL_PAGE_NUM_NPQ_8812 = 0x00 (rtl8812a_hal.h:177-179)
    #   numPubQ = TX_TOTAL_PAGE_NUMBER_8812 - 0x10 - 0x10 - 0x00
    #   TX_TOTAL = 0xFF - BCNQ(0x07) - WOWLAN(0x00) - FW_NDPA(0x02) - DBG(0x00) = 0xF6 (rtl8812a_hal.h:167)
    #   numPubQ = 0xF6-0x20 = 0xD6
    #   _HPQ/_LPQ/_PUBQ/LD_RQPN per hal_com_reg.h:1434-1436,1443
    #   = 0x10 | (0x10<<8) | (0xD6<<16) | BIT31 = 0x80D61010 -> REG_RQPN=0x200 (hal_com_reg.h:171)
    rqpn = ("W32", 0x200, 0x80D61010)
    assert npq in rec.ops
    assert rqpn in rec.ops
    # usb_halinit.c:492 "This step shall be proceed before writting REG_RQPN": NPQ precedes RQPN
    assert _index(rec, npq) < _index(rec, rqpn)


def test_txbuffer_boundary_0xF7():  # branch 6 (+ suspicion flag: boundary=0xF7 not 0xF9)
    rec = _misc()
    # usb_halinit.c:557,563-567 txpktbuf_bndy = TX_PAGE_BOUNDARY_8812 = TX_TOTAL(0xF6)+1 = 0xF7
    #   REG_BCNQ_BDNY=0x424, REG_MGQ_BDNY=0x425, REG_WMAC_LBK_BF_HD=0x45D (hal_com_reg.h:258,259,276),
    #   REG_TRXFF_BNDY=0x114, REG_TDECTRL+1=0x209 (hal_com_reg.h:121,173)
    for addr in (0x424, 0x425, 0x45D, 0x114, 0x209):
        assert ("W8", addr, 0xF7) in rec.ops


def test_queue_priority_three_out_ep_0xF5B0():  # branch 7
    rec = _misc()
    # usb_halinit.c:675-690 3-EP typical: be=LOW(1),bk=LOW(1),vi=NORMAL(2),vo=HIGH(3),mgt=HIGH(3),hi=HIGH(3)
    #   maps (hal_com_reg.h:1400-1405): HIQ3<<14|MGQ3<<12|BKQ1<<10|BEQ1<<8|VIQ2<<6|VOQ3<<4 = 0xF5B0
    #   usb_halinit.c:606-612 value16 = (read16(REG_TRXDMA_CTRL=0x10C) & 0x7) | maps ; read=0 -> 0xF5B0
    assert ("W16", 0x10C, 0xF5B0) in rec.ops
    # usb_halinit.c:693-700,730-751 the 3-EP path never calls init_hi_queue_config ->
    #   REG_HIQ_NO_LMT_EN=0x5A7 (hal_com_reg.h:391) is NOT written
    assert not any(o[0] == "W8" and o[1] == 0x5A7 for o in rec.ops)


@pytest.mark.xfail(reason="OutEpNumber is a device/port constant (ALFA=3 bulk-OUT EPs); the "
                          "4-EP _InitNormalChipFourOutEpPriority path (usb_halinit.c:702-727) is "
                          "not reachable through the transport-only mock.", strict=False)
def test_queue_priority_four_out_ep_0xC5A0():  # branch 8
    rec = _misc()
    # usb_halinit.c:710-716 4-EP typical -> be=1,bk=1,vi=2,vo=2,mgt=0,hi=3 = 0xC5A0 (maps :1400-1405)
    assert ("W16", 0x10C, 0xC5A0) in rec.ops
    # usb_halinit.c:699 also writes REG_HIQ_NO_LMT_EN=0x5A7 = 0xFF on the 4-EP path
    assert ("W8", 0x5A7, 0xFF) in rec.ops


@pytest.mark.xfail(reason="OutEpNumber fixed to 3 in the port; the 2-EP "
                          "_InitNormalChipTwoOutEpPriority path (usb_halinit.c:615-665) is not "
                          "reachable through the transport-only mock.", strict=False)
def test_queue_priority_two_out_ep_0xFAF0():  # branch 9
    rec = _misc()
    # usb_halinit.c:637-661 2-EP with OutEpQueueSel HQ|NQ: valueHi=HIGH(3),valueLow=NORMAL(2)
    #   be=2,bk=2,vi=3,vo=3,mgt=3,hi=3 = 0xFAF0 (maps hal_com_reg.h:1400-1405)
    assert ("W16", 0x10C, 0xFAF0) in rec.ops


@pytest.mark.xfail(reason="OutEpNumber fixed to 3; the 'shall not reach' default branch "
                          "(usb_halinit.c:747-749) emits no register write and cannot be driven "
                          "from the transport mock.", strict=False)
def test_queue_priority_default_no_trxdma_write():  # branch 10
    rec = _misc()
    # usb_halinit.c:747-749 default: RTW_INFO only, no REG_TRXDMA_CTRL write. The 3-EP port always
    # writes 0x10C, so asserting the absence documents that the default path is unreachable here.
    assert not any(o[0] == "W16" and o[1] == 0x10C for o in rec.ops)


def test_page_boundary_rx_dma_0x3E7F():  # branch 11
    rec = _misc()
    # usb_halinit.c:587-588 8812: write16(REG_TRXFF_BNDY+2 = 0x116, RX_DMA_BOUNDARY_8812)
    #   RX_DMA_BOUNDARY_8812 = MAX_RX_DMA_BUFFER_SIZE_8812(0x3E80) - RX_DMA_RESERVED_SIZE_8812(0) - 1
    #   = 0x3E7F (rtl8812a_hal.h:123,134,136)
    assert ("W16", 0x116, 0x3E7F) in rec.ops


def test_transfer_page_size_pbp_512():  # branch 12
    rec = _misc()
    # usb_halinit.c:788-790 value8 = _PSTX(PBP_512) = PBP_512(0x3)<<4 = 0x30 -> REG_PBP=0x104
    #   (hal_com_reg.h:118,1371,1376)
    assert ("W8", 0x104, 0x30) in rec.ops


def test_driver_info_size_0x04():  # branch 13
    rec = _misc()
    # usb_halinit.c:799,1534 write8(REG_RX_DRVINFO_SZ=0x60F, DRVINFO_SZ=4)
    #   (hal_com_reg.h:417, rtw_recv.h:56)
    assert ("W8", 0x60F, 0x04) in rec.ops


def test_interrupt_himr_zeroed():  # branch 14
    rec = _misc()
    # usb_halinit.c:379-380 write32(REG_HIMR0_8812=0xB0, IntrMask[0]=0) ; write32(REG_HIMR1_8812=0xB8, 0)
    #   (rtl8812a_spec.h:45,47 ; IntrMask literally 0)
    assert ("W32", 0x0B0, 0x00000000) in rec.ops
    assert ("W32", 0x0B8, 0x00000000) in rec.ops


def test_network_type_msr_masked_write():  # branch 15
    # usb_halinit.c:774-778 value32 = (read32(REG_CR) & ~MASK_NETTYPE) | _NETTYPE(NT_LINK_AP)
    #   MASK_NETTYPE=0x30000, _NETTYPE(x)=(x&3)<<16, NT_LINK_AP=0x2 (hal_com_reg.h:1358-1362)
    # base read 0 -> just 0x20000 to REG_CR=0x100 (hal_com_reg.h:117)
    rec = _misc()
    assert ("W32", 0x100, 0x00020000) in rec.ops
    # preservation: other REG_CR bits survive the masked write
    rec2 = _misc({0x100: 0xFFFFFFFF})
    # (0xFFFFFFFF & ~0x30000) | 0x20000 = 0xFFFEFFFF
    assert ("W32", 0x100, 0xFFFEFFFF) in rec2.ops


def test_wmac_rcr_and_multicast_and_rxfltmap():  # branch 16
    rec = _misc()
    # usb_halinit.c:813-824 + hal_com.c:3519 straight write32(REG_RCR=0x608, rcr)
    #   rcr = APM|AM|AB|CBSSID_DATA|CBSSID_BCN|APP_ICV|AMF|HTC_LOC_CTRL|APP_MIC|APP_PHYST_RXFF
    #       = BIT1|BIT2|BIT3|BIT6|BIT7|BIT29|BIT13|BIT14|BIT30|BIT28 = 0x700060CE (hal_com_reg.h:1114-1144)
    #   | RCR_APPFCS(BIT31) | FORCEACK(BIT26) = 0xF40060CE (hal_com_reg.h:1114,1607; autoconf.h:246)
    assert ("W32", 0x608, 0xF40060CE) in rec.ops
    # usb_halinit.c:827-828 accept-all multicast: REG_MAR=0x620 and REG_MAR+4=0x624 (hal_com_reg.h:421)
    assert ("W32", 0x620, 0xFFFFFFFF) in rec.ops
    assert ("W32", 0x624, 0xFFFFFFFF) in rec.ops
    # usb_halinit.c:838-843 value16 = BIT10 | BIT5(CONFIG_BEAMFORMING) = 0x420 -> REG_RXFLTMAP1=0x6A2
    #   (hal_com_reg.h:494)
    assert ("W16", 0x6A2, 0x0420) in rec.ops


def test_adaptive_ctrl_rrsr_sifs_retry():  # branch 17
    rec = _misc()
    # usb_halinit.c:863-873 + phydm_rainfo.c:1916-1921 odm_set_mac_reg(R_0x440, 0xfffff, val)
    #   val low-20-bits = RATE_RRSR_WITHOUT_CCK(0xFFFF0) | RATE_RRSR_CCK_ONLY_1M(0xFFFF1) = 0xFFFF1
    #   (hal_com_reg.h:815-816); PHY_SetBBReg8812 masked write (rtl8812a_phycfg.c:68-75), base 0 -> 0xFFFF1
    assert ("W32", 0x440, 0x000FFFF1) in rec.ops
    # usb_halinit.c:879-880 REG_SPEC_SIFS=0x428 = _SPEC_SIFS_CCK(0x10)|_SPEC_SIFS_OFDM(0x10) = 0x1010
    #   (hal_com_reg.h:262,1510-1511)
    assert ("W16", 0x428, 0x1010) in rec.ops
    # usb_halinit.c:883-884 REG_RETRY_LIMIT=0x42A = BIT_LRL(0x30)|BIT_SRL(0x30) = 0x30 | (0x30<<8) = 0x3030
    #   RL_VAL_STA=0x30 (hal_com_reg.h:263,1514-1526)
    assert ("W16", 0x42A, 0x3030) in rec.ops


def test_adaptive_ctrl_rrsr_preserves_upper_bits():  # branch 17 (masked RMW)
    # PHY_SetBBReg8812 with mask 0xfffff preserves bits [31:20] of REG_RRSR (rtl8812a_phycfg.c:68-72)
    rec = _misc({0x440: 0xABC00000})
    # (0xABC00000 & ~0xFFFFF) | 0xFFFF1 = 0xABCFFFF1
    assert ("W32", 0x440, 0xABCFFFF1) in rec.ops


def test_edca_parameters():  # branch 18
    rec = _misc()
    # usb_halinit.c:894-901 SIFS group all 0x100A: REG_SPEC_SIFS=0x428, REG_MAC_SPEC_SIFS=0x63A,
    #   REG_SIFS_CTX=0x514, REG_SIFS_TRX=0x516 (hal_com_reg.h:262,427,345,346)
    for addr in (0x428, 0x63A, 0x514, 0x516):
        assert ("W16", addr, 0x100A) in rec.ops
    # usb_halinit.c:904-907 TXOP params (hal_com_reg.h:338-341)
    assert ("W32", 0x508, 0x005EA42B) in rec.ops  # REG_EDCA_BE_PARAM
    assert ("W32", 0x50C, 0x0000A44F) in rec.ops  # REG_EDCA_BK_PARAM
    assert ("W32", 0x504, 0x005EA324) in rec.ops  # REG_EDCA_VI_PARAM
    assert ("W32", 0x500, 0x002FA226) in rec.ops  # REG_EDCA_VO_PARAM
    # usb_halinit.c:910-911 REG_USTIME_TSF=0x55C, REG_USTIME_EDCA=0x638 both 0x50 (hal_com_reg.h:380,426)
    assert ("W8", 0x55C, 0x50) in rec.ops
    assert ("W8", 0x638, 0x50) in rec.ops


def test_adaptive_ctrl_before_edca_on_0x428():  # branch 17/18 ordering
    rec = _misc()
    # AdaptiveCtrl (usb_halinit.c:1539) writes REG_SPEC_SIFS=0x1010 BEFORE EDCA (:1540) writes 0x100A
    first = ("W16", 0x428, 0x1010)
    second = ("W16", 0x428, 0x100A)
    assert _index(rec, first) < _index(rec, second)


def test_retry_function():  # branch 19
    rec = _misc()
    # usb_halinit.c:961-963 write8(REG_FWHW_TXQ_CTRL=0x420, read8 | EN_AMPDU_RTY_NEW=BIT7)
    #   (hal_com_reg.h:256,1506) base 0 -> 0x80
    assert ("W8", 0x420, 0x80) in rec.ops
    # usb_halinit.c:967 write8(REG_ACKTO=0x640, 0x80) (hal_com_reg.h:432)
    assert ("W8", 0x640, 0x80) in rec.ops


def test_usb_agg_tx_descnum():  # branch 20
    rec = _misc()
    # usb_halinit.c:998-1002 value32 = (read32(REG_DWBCN0_CTRL_8812=REG_TDECTRL=0x208) &
    #   ~(BLK_DESC_NUM_MASK<<BLK_DESC_NUM_SHIFT)) | ((UsbTxAggDescNum & 0xF)<<4)
    #   masks 0xF<<4, UsbTxAggDescNum=0x01 (8812AU) -> base 0 -> 0x10 (hal_com_reg.h:173,1447-1448)
    assert ("W32", 0x208, 0x00000010) in rec.ops
    # usb_halinit.c:1003-1004 REG_DWBCN1_CTRL write is 8821U-only -> 8812AU must NOT write REG_DWBCN1(0x228)
    assert not any(o[0].startswith("W") and o[1] == 0x228 for o in rec.ops)
    # usb_halinit.c:259-263 CONFIG_USB_TX_AGGREGATION ON -> the #else write8(REG_TDECTRL=0x208, 0x10)
    #   is compiled out: no byte write of 0x10 to 0x208
    assert ("W8", 0x208, 0x10) not in rec.ops


def test_usb_agg_tx_descnum_preserves_low_nibble():  # branch 20 (masked RMW)
    rec = _misc({0x208: 0xFF})
    # (0xFF & ~0xF0) | (0x01<<4) = 0x0F | 0x10 = 0x1F (usb_halinit.c:999-1000)
    assert ("W32", 0x208, 0x0000001F) in rec.ops


def test_usb_agg_rx_hs():  # branch 21
    rec = _misc()
    # usb_halinit.c:1056-1057 temp = rxagg_usb_size(0x5) | (rxagg_usb_timeout(0x20) << 8) = 0x2005
    #   -> REG_RXDMA_AGG_PG_TH=0x280 (hal_com_reg.h:187 ; sizes usb_halinit.c:191-192)
    pg_th = ("W16", 0x280, 0x2005)
    # usb_halinit.c:1051,1067 write8(REG_TRXDMA_CTRL=0x10C, valueDMA | RXDMA_AGG_EN=BIT2)
    #   (hal_com_reg.h:120,1383) base 0 -> 0x04
    trxdma = ("W8", 0x10C, 0x04)
    assert pg_th in rec.ops
    assert trxdma in rec.ops
    # usb_halinit.c:1057 then :1067 -> PG_TH written before the TRXDMA_CTRL RMW
    assert _index(rec, pg_th) < _index(rec, trxdma)


def test_beacon_parameters():  # branch 22
    rec = _misc()
    # rtl8812a_hal_init.c:3565-3573 val16 = DIS_TSF_UDT | (DIS_TSF_UDT<<8), DIS_TSF_UDT=BIT4
    #   = 0x10 | 0x1000 = 0x1010 -> REG_BCN_CTRL=0x550 (hal_com_reg.h:372,1548)
    assert ("W16", 0x550, 0x1010) in rec.ops
    # rtl8812a_hal_init.c:3576 REG_TBTT_PROHIBIT=0x540 = TBTT_PROHIBIT_SETUP_TIME=0x04 (hal_com.h:306)
    assert ("W8", 0x540, 0x04) in rec.ops
    # rtl8812a_hal_init.c:3579 0x541 = TBTT_PROHIBIT_HOLD_TIME_STOP_BCN & 0xFF = 0x64 (hal_com.h:308)
    assert ("W8", 0x541, 0x64) in rec.ops
    # rtl8812a_hal_init.c:3583 REG_DRVERLYINT=0x558 = DRIVER_EARLY_INT_TIME_8812=0x05 (rtl8812a_hal.h:118)
    assert ("W8", 0x558, 0x05) in rec.ops
    # rtl8812a_hal_init.c:3584 REG_BCNDMATIM=0x559 = BCN_DMA_ATIME_INT_TIME_8812=0x02 (rtl8812a_hal.h:119)
    assert ("W8", 0x559, 0x02) in rec.ops
    # rtl8812a_hal_init.c:3588 REG_BCNTCFG=0x510 = 0x4413 (hal_com_reg.h:342)
    assert ("W16", 0x510, 0x4413) in rec.ops


def test_beacon_params_tbtt_hold_high_nibble_masked():  # branch 22 (masked RMW)
    rec = _misc({0x542: 0xFF})
    # rtl8812a_hal_init.c:3580-3581 0x542 = (read8 & 0xF0) | (0x64 >> 8 = 0x00) -> 0xF0
    assert ("W8", 0x542, 0xF0) in rec.ops


def test_beacon_max_error_0xFF():  # branch 23
    rec = _misc()
    # usb_halinit.c:921-922 CONFIG_ADHOC_WORKAROUND_SETTING(=1) -> write8(REG_BCN_MAX_ERR=0x55D, 0xFF)
    #   (hal_com_reg.h:381, autoconf.h:254)
    assert ("W8", 0x55D, 0xFF) in rec.ops


def test_burst_head():  # branch 24
    rec = _misc()
    # usb_halinit.c:212-214
    assert ("W8", 0xF050, 0x01) in rec.ops                 # usb3 rx interval
    assert ("W16", 0x288, 0x7400) in rec.ops               # REG_RXDMA_STATUS burst len (hal_com_reg.h:189)
    assert ("W8", 0x289, 0xF5) in rec.ops                  # rxdma control


def test_ampdu_max_time_0x70():  # branch 25
    rec = _misc()
    # usb_halinit.c:220-221 !8821U -> write8(REG_AMPDU_MAX_TIME_8812=0x456, 0x70) (rtl8812a_spec.h:125)
    assert ("W8", 0x456, 0x70) in rec.ops
    # the 8821U value 0x5e (usb_halinit.c:219) must NOT appear
    assert ("W8", 0x456, 0x5E) not in rec.ops


def test_burst_mid():  # branch 26
    rec = _misc()
    # usb_halinit.c:223-225
    assert ("W32", 0x458, 0xFFFFFFFF) in rec.ops           # REG_AMPDU_MAX_LENGTH_8812 (rtl8812a_spec.h:128)
    assert ("W8", 0x55C, 0x50) in rec.ops                  # REG_USTIME_TSF
    assert ("W8", 0x638, 0x50) in rec.ops                  # REG_USTIME_EDCA


def test_speed_source_reads_0xFF():  # branch 27
    rec = _misc()
    # usb_halinit.c:227-230 8812AU (not 8821U) reads device speed from 0xFF (SS marker in bit7)
    assert ("R8", 0x00FF) in rec.ops


def test_burst_usb2_512b():  # branch 28
    # usb_halinit.c:232-237 speedvalue&BIT7 (USB2) AND ((read8(0xFE17)>>4)&0x03)==0 -> 512B
    #   write8(REG_RXDMA_PRO_8812=0x290, (provalue | BIT4|BIT3|BIT2|BIT1) & ~BIT5) (rtl8812a_spec.h:88)
    rec = _misc({0x290: 0x00})
    # (0 | 0x1E) & ~0x20 = 0x1E
    assert ("W8", 0x290, 0x1E) in rec.ops


def test_burst_usb2_512b_clears_bit5():  # branch 28 (mask proof)
    rec = _misc({0x290: 0x20})
    # (0x20 | 0x1E) & ~0x20 = 0x3E & 0xDF = 0x1E : BIT5 cleared
    assert ("W8", 0x290, 0x1E) in rec.ops


def test_burst_usb2_64b():  # branch 29
    # usb_halinit.c:238-242 USB2 AND ((read8(0xFE17)>>4)&0x03)!=0 -> 64B
    #   write8(0x290, (provalue | BIT5|BIT3|BIT2|BIT1) & ~BIT4)
    rec = _misc({0xFE17: 0x10, 0x290: 0x00})
    # (0 | 0x2E) & ~0x10 = 0x2E
    assert ("W8", 0x290, 0x2E) in rec.ops


def test_burst_usb3():  # branch 30
    # usb_halinit.c:248-257 speedvalue&BIT7 clear (USB3) ->
    #   write8(0x290, (provalue | BIT3|BIT2|BIT1) & ~(BIT5|BIT4)) ; write8(0xf008, read8(0xf008) & 0xE7)
    rec = _misc({0x00FF: 0x00, 0x290: 0x00, 0xF008: 0x00})
    # (0 | 0x0E) & ~0x30 = 0x0E
    assert ("W8", 0x290, 0x0E) in rec.ops
    assert ("W8", 0xF008, 0x00) in rec.ops


def test_reset_8051_low_byte_unchanged():  # branch 31
    # usb_halinit.c:265-266 write8(REG_SYS_FUNC_EN=0x02, read8 & ~BIT(10)); BIT10 is above the low
    #   byte so the u8 write is unchanged (hal_com_reg.h:40)
    rec = _misc({0x02: 0x53})
    assert ("W8", 0x02, 0x53) in rec.ops


def test_single_ampdu_pktlimit_pifs():  # branch 32
    rec = _misc()
    # usb_halinit.c:268 write8(REG_HT_SINGLE_AMPDU_8812=0x4C7, read8 | BIT7) base 0 -> 0x80 (rtl8812a_spec.h:140)
    assert ("W8", 0x4C7, 0x80) in rec.ops
    # usb_halinit.c:269 write8(REG_RX_PKT_LIMIT=0x60C, 0x18) (hal_com_reg.h:415)
    assert ("W8", 0x60C, 0x18) in rec.ops
    # usb_halinit.c:271 write8(REG_PIFS=0x512, 0x00) (hal_com_reg.h:343)
    assert ("W8", 0x512, 0x00) in rec.ops


def test_max_aggr_tail():  # branch 33
    rec = _misc()
    # usb_halinit.c:279 write16(REG_MAX_AGGR_NUM=0x4CA, 0x1F1F) (hal_com_reg.h:302)
    assert ("W16", 0x4CA, 0x1F1F) in rec.ops
    # usb_halinit.c:280 write8(REG_FWHW_TXQ_CTRL=0x420, read8 & ~BIT7) base 0 -> 0x00 (clears branch-19 set)
    assert ("W8", 0x420, 0x00) in rec.ops
    # usb_halinit.c:277 the 8821U-only REG_FAST_EDCA_CTRL=0x460 write must NOT happen (hal_com_reg.h:277)
    assert not any(o[0].startswith("W") and o[1] == 0x460 for o in rec.ops)


def test_retry_set_before_max_aggr_clear_on_0x420():  # branch 19/33 ordering
    rec = _misc()
    # _InitRetryFunction (usb_halinit.c:1542) sets BIT7 BEFORE _InitBurstPktLen (:1548,:280) clears it
    set_bit = ("W8", 0x420, 0x80)
    clr_bit = ("W8", 0x420, 0x00)
    assert _index(rec, set_bit) < _index(rec, clr_bit)


def test_ampdu_burst_mode_not_written():  # branch 34
    rec = _misc()
    # usb_halinit.c:283-284 pHalData->AMPDUBurstMode is never assigned (FALSE) -> REG_AMPDU_BURST_MODE_8812
    #   (0x4BC) must NOT be written (rtl8812a_spec.h:139)
    assert not any(o[0].startswith("W") and o[1] == 0x4BC for o in rec.ops)


def test_rsv_ctrl_and_arfb_tables():  # branch 35
    rec = _misc()
    # usb_halinit.c:286 write8(REG_RSV_CTRL=0x1C, read8 | BIT5 | BIT6) base 0 -> 0x60 (hal_com_reg.h:52)
    assert ("W8", 0x1C, 0x60) in rec.ops
    # usb_halinit.c:289-301 ARFB tables (rtl8812a_spec.h:122,123,136,137)
    assert ("W32", 0x444, 0x00000010) in rec.ops           # REG_ARFR0_8812
    assert ("W32", 0x448, 0xFFFFF000) in rec.ops           # REG_ARFR0_8812+4
    assert ("W32", 0x44C, 0x00000010) in rec.ops           # REG_ARFR1_8812
    assert ("W32", 0x450, 0x003FF000) in rec.ops           # REG_ARFR1_8812+4
    assert ("W32", 0x48C, 0x00000015) in rec.ops           # REG_ARFR2_8812
    assert ("W32", 0x490, 0x003FF000) in rec.ops           # REG_ARFR2_8812+4
    assert ("W32", 0x494, 0x00000015) in rec.ops           # REG_ARFR3_8812
    assert ("W32", 0x498, 0xFFCFF000) in rec.ops           # REG_ARFR3_8812+4


def test_cr_enable_is_last_write():  # branch 36
    rec = _misc()
    # usb_halinit.c:1555-1556 value8 = read8(REG_CR=0x100); write8(REG_CR, value8 | MACTXEN | MACRXEN)
    #   MACTXEN=BIT6, MACRXEN=BIT7 (hal_com_reg.h:1351-1352) base 0 -> 0xC0; LAST write of mac_init_misc
    assert _writes(rec)[-1] == ("W8", 0x100, 0xC0)


# =====================================================================================
# hal_init_misc_pre  --  post-tune turn-on tail, section 1a
# =====================================================================================

def test_pre_invalidate_cam_all():  # branch 37
    rec = Rec()
    hal_init_misc_pre(rec)
    # usb_halinit.c:1601 invalidate_cam_all -> HW_VAR_CAM_INVALID_ALL (rtl8812a_hal_init.c:4203-4205)
    #   write32(REG_CAMCMD=0x670, BIT31|BIT30 = 0xC0000000) (hal_com_reg.h:480)
    assert ("W32", 0x670, 0xC0000000) in rec.ops


def test_pre_hwseq_bar_nav():  # branch 38
    rec = Rec()
    hal_init_misc_pre(rec)
    # usb_halinit.c:1606 write8(REG_HWSEQ_CTRL=0x423, 0xFF) (hal_com_reg.h:257)
    assert ("W8", 0x423, 0xFF) in rec.ops
    # usb_halinit.c:1612 write32(REG_BAR_MODE_CTRL=0x4CC, 0x0201FFFF) (hal_com_reg.h:304)
    assert ("W32", 0x4CC, 0x0201FFFF) in rec.ops
    # usb_halinit.c:1625 write8(0x652, 0x00) (NAV limit, literal address)
    assert ("W8", 0x652, 0x00) in rec.ops


def test_pre_fast_edca_not_written():  # branch 39
    rec = Rec()
    hal_init_misc_pre(rec)
    # usb_halinit.c:1614-1615 REG_FAST_EDCA_CTRL(0x460) is written only if registrypriv.wifi_spec;
    #   wifi_spec=0 -> absence-of-write (hal_com_reg.h:277)
    assert not any(o[0].startswith("W") and o[1] == 0x460 for o in rec.ops)


# =====================================================================================
# hal_init_misc_post  --  post-tune turn-on tail, section 1b (non-MP else block)
# =====================================================================================

def test_post_non_mp_turn_on_writes():  # branch 40 + 41
    # usb_halinit.c:1650-1663 (non-MP else block; MP_DRIVER=0 so MPT_InitializeAdapter not taken)
    rec = Rec(reads={0x4C6: 0xFF})
    hal_init_misc_post(rec)
    # usb_halinit.c:1650 write8(REG_QUEUE_CTRL=0x4C6, read8 & 0xF7) : 0xFF & 0xF7 = 0xF7 (BIT3 cleared)
    #   (hal_com_reg.h:299)
    assert ("W8", 0x4C6, 0xF7) in rec.ops
    # usb_halinit.c:1653 write8(REG_FWHW_TXQ_CTRL+1 = 0x421, 0x0F)
    assert ("W8", 0x421, 0x0F) in rec.ops
    # usb_halinit.c:1656 write8(REG_EARLY_MODE_CONTROL_8812+3 = 0x2BF, 0x01) (rtl8812a_spec.h:89)
    assert ("W8", 0x2BF, 0x01) in rec.ops
    # usb_halinit.c:1659 write16(REG_TX_RPT_TIME=0x4F0, 0x3DF0) (hal_com_reg.h:330)
    assert ("W16", 0x4F0, 0x3DF0) in rec.ops
    # usb_halinit.c:1662 write8(REG_SDIO_CTRL_8812=0x70, 0x00) (rtl8812a_spec.h:40)
    assert ("W8", 0x70, 0x00) in rec.ops
    # usb_halinit.c:1663 write8(REG_ACLK_MON=0x3E, 0x00) (hal_com_reg.h:67)
    assert ("W8", 0x3E, 0x00) in rec.ops


# =====================================================================================
# Suspicion flags  --  MISC31 writes the C oracle performs but the Python MAC fns omit
# =====================================================================================

@pytest.mark.xfail(reason="NOT A BUG (triaged 2026-09-12): MISC31 REG_USB_HRPWM(0xFE58)=0 "
                          "(usb_halinit.c:1710) IS ported -- in monitor.set_monitor_mode "
                          "(monitor.py:91), not hal_init_misc_post. This port relocates the C's "
                          "end-of-hal_init MISC31 stage into monitor entry. Out of scope for the "
                          "mac functions; belongs to monitor-area tests.",
                   strict=False)
def test_misc31_usb_hrpwm_written():  # branch 42
    rec = Rec()
    hal_init_misc_post(rec)
    # usb_halinit.c:1710 rtw_write8(REG_USB_HRPWM=0xFE58, 0) (hal_com_reg.h:545)
    assert ("W8", 0xFE58, 0x00) in rec.ops


@pytest.mark.xfail(reason="NOT A BUG (triaged 2026-09-12): the XMIT_ACK REG_FWHW_TXQ_CTRL|=BIT12 "
                          "write (usb_halinit.c:1712-1715) IS ported -- monitor.set_monitor_mode "
                          "(monitor.py:92). Not in the mac functions by design (MISC31 relocated to "
                          "monitor entry). Out of scope for mac; belongs to monitor-area tests.",
                   strict=False)
def test_misc31_xmit_ack_bit12_set():  # branch 43
    rec = Rec(reads={0x420: 0x00})
    hal_init_misc_post(rec)
    # usb_halinit.c:1712-1715 rtw_write32(REG_FWHW_TXQ_CTRL=0x420, read32 | BIT(12)) -> 0x1000
    assert ("W32", 0x420, 0x00001000) in rec.ops


# =====================================================================================
# Pre-MAC context branches  --  handled by other Python modules, not the MAC functions
# =====================================================================================

@pytest.mark.xfail(reason="bkeepfwalive 'goto exit' early-exit (usb_halinit.c:1400-1414) is a "
                          "warm-reattach concern; the cold-boot mac_init_misc cannot be told to "
                          "skip all writes via the transport-only mock.", strict=False)
def test_bkeepfwalive_early_exit_skips_all_writes():  # branch 44
    rec = _misc()
    assert not any(o[0].startswith("W") for o in rec.ops)


@pytest.mark.xfail(reason="RF-path reset (usb_halinit.c:1436-1441) runs before PHY_MACConfig and "
                          "belongs to the RF/power bring-up module, not the mac.py functions "
                          "under test.", strict=False)
def test_rf_reset_writes_present():  # branch 45
    rec = Rec()
    for fn in (phy_mac_config, mac_init_misc, hal_init_misc_pre, hal_init_misc_post):
        fn(rec)
    # usb_halinit.c:1437-1440 write8(REG_RF_CTRL=0x1F, 5/7); write8(REG_RF_B_CTRL_8812=0x76, 5/7)
    #   (hal_com_reg.h:53, rtl8812a_spec.h:42)
    assert ("W8", 0x1F, 7) in rec.ops
    assert ("W8", 0x76, 7) in rec.ops


@pytest.mark.xfail(reason="drop-incorrect-bulkout (usb_halinit.c:755-765,1477) fires in the "
                          "LLT/pre-FW stage, handled by another Python module, not the MAC "
                          "functions.", strict=False)
def test_drop_incorrect_bulkout_present():  # branch 46
    rec = Rec()
    for fn in (phy_mac_config, mac_init_misc, hal_init_misc_pre, hal_init_misc_post):
        fn(rec)
    # usb_halinit.c:761-763 write32(REG_TXDMA_OFFSET_CHK=0x20C, read32 | DROP_DATA_EN=BIT9)
    #   (hal_com_reg.h:174,1452)
    assert any(o[0] == "W32" and o[1] == 0x20C for o in rec.ops)
