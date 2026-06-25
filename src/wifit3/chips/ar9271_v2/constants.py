"""AR9271 (ath9k_htc) register/protocol constants, ported verbatim from the v6.18 kernel.

Citations are to ``data_dumps/ath9k-source-v6.18/ath9k/`` — file:line at the v6.18 tag.
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
