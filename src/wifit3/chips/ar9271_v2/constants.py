"""AR9271 (ath9k_htc) register/protocol constants, ported verbatim from the v6.18.12 kernel.

Citations are to ``driver_sources/ath9k-source-v6.18.12/`` — file:line at the v6.18.12 tag.
Never type a value from memory; every constant here is grepped out of the C source.
"""
from __future__ import annotations

# ---- USB identity ----------------------------------------------------------
AR9271_VID = 0x0cf3
AR9271_PID = 0x9271        # [SRC] hif_usb.c ath9k_hif_usb_ids[] (AR9271 driver_info=0)

# ---- Firmware download (cold boot) -----------------------------------------
# [SRC] hif_usb.h:22-48 + hif_usb.c:1068 ath9k_hif_usb_download_fw.
FIRMWARE_NAME = "htc_9271-1.4.0.fw"   # [SRC] hif_usb.h:32 HTC_9271_MODULE_FW (MAJOR=1, MINOR_IDX_MAX=4)
FIRMWARE_DOWNLOAD = 0x30              # [SRC] hif_usb.h:47 bRequest, per-chunk RAM write
FIRMWARE_DOWNLOAD_COMP = 0x31         # [SRC] hif_usb.h:48 bRequest, "jump to text" boot
AR9271_FIRMWARE = 0x501000            # [SRC] hif_usb.h:43 load address (RAM)
AR9271_FIRMWARE_TEXT = 0x903000       # [SRC] hif_usb.h:44 text entry point
FW_CHUNK = 4096                       # [SRC] hif_usb.c:1074/1081 kzalloc(4096) + min(len,4096)
USB_MSG_TIMEOUT = 1000                # [SRC] hif_usb.h:74 (ms)

# Minimum supported firmware, ported from ath9k_init_firmware_version [SRC] htc.h
# MAJOR_VERSION_REQ / MINOR_VERSION_REQ. get_fw_version reports (1, 4) for htc_9271-1.4.0.fw.
FW_VERSION_MAJOR_REQ = 1
FW_VERSION_MINOR_REQ = 3

# bmRequestType for the two FW control writes: 0x40 | USB_DIR_OUT.
# USB_DIR_OUT == 0x00, so the byte on the wire is 0x40 (vendor, host->device).
# [SRC] hif_usb.c:1086,1110.
BMREQ_VENDOR_OUT = 0x40

# ---- USB endpoints ---------------------------------------------------------
# ath9k_htc names four logical pipes by endpoint NUMBER; the probe asserts each
# endpoint's number matches [SRC] hif_usb.c:1367-1370. Addresses below add the
# direction bit (IN = 0x80) the silicon actually exposes (confirmed on the wire).
USB_WLAN_TX_PIPE = 1       # [SRC] hif_usb.h:69  bulk OUT — HTC/TX frames  -> ep 0x01
USB_WLAN_RX_PIPE = 2       # [SRC] hif_usb.h:70  bulk IN  — HIF RX stream  -> ep 0x82
USB_REG_IN_PIPE = 3        # [SRC] hif_usb.h:71  int  IN  — WMI/HTC ctrl   -> ep 0x83
USB_REG_OUT_PIPE = 4       # [SRC] hif_usb.h:72  int  OUT — WMI/HTC ctrl   -> ep 0x04

EP_WLAN_TX = 0x01
EP_WLAN_RX = 0x82
EP_REG_IN = 0x83
EP_REG_OUT = 0x04

# ---- HTC (host-target communication) ---------------------------------------
# [SRC] htc_hst.h:42-53,132-139,164-172.
ENDPOINT0 = 0                          # the reserved HTC control endpoint
ENDPOINT_MAX = 22
ENDPOINT_UNUSED = -1

HTC_MSG_READY_ID = 1                   # device->host: target ready (credits, credit_size)
HTC_MSG_CONNECT_SERVICE_ID = 2
HTC_MSG_CONNECT_SERVICE_RESPONSE_ID = 3
HTC_MSG_SETUP_COMPLETE_ID = 4
HTC_MSG_CONFIG_PIPE_ID = 5
HTC_MSG_CONFIG_PIPE_RESPONSE_ID = 6

HTC_FRAME_HDR_LEN = 8                  # endpoint_id, flags, be16 payload_len, control[4]
HTC_SERVICE_SUCCESS = 0                # [SRC] htc_hst.h:185

# Service IDs: MAKE_SERVICE_ID(WMI_SERVICE_GROUP=1, index) [SRC] htc_hst.h:164-172.
WMI_CONTROL_SVC = 0x0100
WMI_BEACON_SVC = 0x0101
WMI_CAB_SVC = 0x0102
WMI_UAPSD_SVC = 0x0103
WMI_MGMT_SVC = 0x0104
WMI_DATA_VO_SVC = 0x0105
WMI_DATA_VI_SVC = 0x0106
WMI_DATA_BE_SVC = 0x0107
WMI_DATA_BK_SVC = 0x0108

# AR9271 advertises 33 host->target credits (AR7010 uses 45) [SRC] htc_drv_init.c:206-208.
HTC_CREDITS_AR9271 = 33

# service_id -> (ul_pipe, dl_pipe). WMI control rides the REG pipes; everything else the bulk
# WLAN pipes [SRC] htc_hst.c:50-86 service_to_ulpipe / service_to_dlpipe.
SERVICE_PIPES = {
    WMI_CONTROL_SVC: (USB_REG_OUT_PIPE, USB_REG_IN_PIPE),
}
for _svc in (WMI_BEACON_SVC, WMI_CAB_SVC, WMI_UAPSD_SVC, WMI_MGMT_SVC,
             WMI_DATA_VO_SVC, WMI_DATA_VI_SVC, WMI_DATA_BE_SVC, WMI_DATA_BK_SVC):
    SERVICE_PIPES[_svc] = (USB_WLAN_TX_PIPE, USB_WLAN_RX_PIPE)

# Connect order: WMI control first (ath9k_wmi_connect), then the data services
# [SRC] htc_drv_init.c:140-198 ath9k_init_htc_services.
SERVICE_CONNECT_ORDER = [
    WMI_CONTROL_SVC, WMI_BEACON_SVC, WMI_CAB_SVC, WMI_UAPSD_SVC, WMI_MGMT_SVC,
    WMI_DATA_BE_SVC, WMI_DATA_BK_SVC, WMI_DATA_VI_SVC, WMI_DATA_VO_SVC,
]
