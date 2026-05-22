# =============================================================================
# Atheros AR9271 (ath9k_htc) Protocol Constants
# =============================================================================
# Formal definitions for WMI and HTC layers as used in firmware v1.4.
# Documentation based on mainline ath9k_htc and reverse-engineered dumps.

# --- WMI Command IDs (Host -> Target) ---
WMI_ECHO_CMDID             = 0x0001
WMI_GET_FW_VERSION         = 0x0003
WMI_DISABLE_INTR_CMDID     = 0x0004
WMI_ENABLE_INTR_CMDID      = 0x0005
WMI_ATH_INIT_CMDID         = 0x0006  # Cold boot radio init
WMI_DRAIN_TXQ_ALL_CMDID    = 0x000B
WMI_START_RECV_CMDID       = 0x000C
WMI_STOP_RECV_CMDID        = 0x000D
WMI_FLUSH_RECV_CMDID       = 0x000E
WMI_SET_MODE_CMDID         = 0x000F  # 1=STA, 2=AP, 3=IBSS
WMI_NODE_CREATE_CMDID      = 0x0010
WMI_VAP_CREATE_CMDID       = 0x0013
WMI_REG_READ_CMDID         = 0x0014
WMI_REG_WRITE_CMDID        = 0x0015
WMI_SET_RX_FILTER_CMDID    = 0x0012
WMI_TARGET_IC_UPDATE_CMDID = 0x0019
WMI_REG_RMW_CMDID          = 0x0020

# --- WMI Event IDs (Target -> Host) ---
# Note: v1.4 firmware (current hardware) has variance in event IDs:
# 1. On Interrupt Pipe (EP 0x83), READY arrives as 0x0001.
# 2. On Bulk Pipe (EP 0x82), READY arrives as 0x1001.
# 3. On Bulk Pipe (EP 0x82), radio packets use several IDs: 0x1002, 0x0400, 0x0000.
WMI_READY_EVENTID          = 0x1001  # Standard / Bulk version
WMI_READY_EP0_ID           = 0x0001  # v1.4 Interrupt version
WMI_RECV_PDU_EVENTID       = 0x1002  # Standard RX event ID
WMI_RECV_PDU_V14_ID        = 0x0400  # verified from hex dumps on v1.4
WMI_RECV_PDU_V14_BCN_ID    = 0x0000  # verified for beacons on v1.4
WMI_SWBA_EVENTID           = 0x1002  # mainline equivalent
WMI_REG_RSP_EVENTID        = 0x0013  # ACK for reg write
WMI_HWR_MODE_RESP_EVENTID  = 0x0014  # ACK for reg read

# Mainline/v1.3+ equivalents
WMI_TGT_RDY_EVENTID        = 0x1001 

# --- HTC Endpoints (Logical) ---
HTC_ENDPOINT_CONTROL       = 0
HTC_ENDPOINT_WMI           = 1
HTC_ENDPOINT_WLAN_DATA     = 2

# --- USB Endpoints (Physical AR9271) ---
USB_EP_WMI_CMD_OUT         = 0x04  # Bulk OUT
USB_EP_HTC_CTRL_IN         = 0x83  # Interrupt IN (Control/Ready)
USB_EP_DATA_WMI_IN         = 0x82  # Bulk IN (Data/Events)

# --- HIF stream constants (per ath9k-source-v6.18/hif_usb.h:50-51) ---
# 4-byte HIF header on Bulk pipes: [pkt_len: LE16][pkt_tag: LE16]
ATH_USB_RX_STREAM_MODE_TAG = 0x4e00   # tag on every RX HIF chunk
ATH_USB_TX_STREAM_MODE_TAG = 0x697e   # tag on every TX HIF chunk
HIF_MAX_RX_BUF_SIZE        = 16384    # ath9k-source-v6.18/hif_usb.h:60

# --- Calibration Constants ---
AR_PHY_SYNTH_CONTROL       = 0x9874
AR_PHY_RESET               = 0x9860
AR_SILICON_REV_REG         = 0x4020
