"""RTL8922AU TX: build the rtw89 BE v2 TX descriptor for a management frame.

The descriptor is 8 body words + 8 info words = 64 bytes, prepended to the FCS-less MPDU (the hw
computes and appends the FCS). A monitor-vif MGMT frame is macid 0, no security, QSEL B0_MGMT,
ch_dma CH8, lowest basic rate. [SRC] core.c:1653-1804 fill_txdesc_v2, txrx.h:154-342 BE_TXD_*.
"""
import struct

HW_RATE_CCK1 = 0x0                       # RTW89_HW_RATE_CCK1 (2.4 GHz mgmt basic rate). core.h:508
HW_RATE_OFDM6 = 0x4                      # RTW89_HW_RATE_OFDM6 (5 GHz mgmt basic rate). core.h:512
QSEL_B0_MGMT = 0x12                      # RTW89_TX_QSEL_B0_MGMT. txrx.h:819
TXCH_CH8 = 8                            # RTW89_TXCH_CH8 = B0MG ch_dma. txrx.h:780
BULKOUT_ID_B0MG = 0                     # out_pipe index for the mgmt DMA channel. rtw8922au.c:23


def build_tx_desc_mgmt(mpdu: bytes, *, band_is_2g: bool = True, ack: bool = True) -> bytes:
    """rtw89_core_fill_txdesc_v2 for a monitor-vif MGMT frame. `ack=False` sets NO_ACK (the
    aireplay-style no-ACK path); the default requests an ACK. [SRC] core.c:864, 1227, 1653-1802."""
    seq = (int.from_bytes(mpdu[22:24], "little") >> 4) & 0xFFF if len(mpdu) >= 24 else 0
    is_bmc = len(mpdu) >= 5 and (mpdu[4] & 0x01)         # addr1 group (broadcast/multicast) bit
    rate = HW_RATE_CCK1 if band_is_2g else HW_RATE_OFDM6
    w = [0] * 16
    w[0] = (1 << 22) | (TXCH_CH8 << 16) | (1 << 10) | (1 << 7)   # WDINFO_EN, CH_DMA, STF_MODE, WD_PAGE
    w[2] = (QSEL_B0_MGMT << 17) | (len(mpdu) & 0x3FFF)           # QSEL, TXPKTSIZE
    w[3] = (1 << 13) | (seq & 0xFFF)                             # IS_MLD_SW_EN, WIFI_SEQ
    w[6] = 0 if ack else (1 << 12)                              # BODY6 NO_ACK
    w[7] = (1 << 31) | ((rate & 0xFFF) << 16)                    # USERATE_SEL, DATARATE
    w[8] = 1 << 10                                              # INFO0 DISDATAFB
    w[12] = (1 << 31) | (0 if is_bmc else (1 << 27))            # INFO4 HW_RTS_EN, RTS_EN = !is_bmc
    return struct.pack("<16I", *w)
