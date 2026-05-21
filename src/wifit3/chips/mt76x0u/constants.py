"""MT76x0U register addresses, vendor requests, and bit masks.

Every constant here is grepped verbatim from data_dumps/mt76-source-v6.18/
and cross-checked against capture-2.pcap WIRE evidence. See MT76X0U.md for
the verification table. Do NOT add a symbol here without a corresponding
[SRC] line in the kernel and (where applicable) a [WIRE] confirmation.

Per [[feedback_prefer_fork_over_base]] this module is INTENTIONALLY a sibling
of chips/mt76x2u/constants.py — duplication is fine, do NOT extract to a
shared `mt76x02_base/` module until 2+ feature-complete siblings exist.
"""
from __future__ import annotations

# ============================================================
# USB device id_table — ported 1:1 from data_dumps/mt76-source-v6.18/
# mt76x0/usb.c:14-43. Format matches what test_hw_mt76x0u.py uses.
# ============================================================
USB_IDS_MT76X0U: list[tuple[int, int, str]] = [
    (0x148F, 0x7610, "MediaTek MT7610U reference"),
    (0x13B1, 0x003E, "Linksys AE6000"),
    (0x0E8D, 0x7610, "Sabrent NTWLAC / MediaTek MT7610U"),
    (0x7392, 0xa711, "Edimax 7711mac"),
    (0x7392, 0xb711, "Edimax / Elecom"),
    (0x148F, 0x761a, "TP-Link TL-WDN5200"),
    (0x148F, 0x760a, "TP-Link (unknown)"),
    (0x0B05, 0x17d1, "Asus USB-AC51"),
    (0x0B05, 0x17db, "Asus USB-AC50"),
    (0x0DF6, 0x0075, "Sitecom WLA-3100"),
    (0x2019, 0xab31, "Planex GW-450D"),
    (0x2001, 0x3d02, "D-Link DWA-171 rev B1"),
    (0x0586, 0x3425, "Zyxel NWD6505"),
    (0x07B8, 0x7610, "AboCom AU7212"),
    (0x04BB, 0x0951, "I-O DATA WN-AC433UK"),
    (0x057C, 0x8502, "AVM FRITZ!WLAN USB Stick AC 430"),
    (0x293C, 0x5702, "Comcast Xfinity KXW02AAA"),
    (0x20F4, 0x806b, "TRENDnet TEW-806UBH"),
    (0x7392, 0xc711, "Devolo Wifi ac Stick"),
    (0x0DF6, 0x0079, "Sitecom Europe ac Stick"),
    (0x2357, 0x0123, "TP-Link T2UHP_US_v1"),
    (0x2357, 0x010b, "TP-Link T2UHP_UN_v1"),
    (0x2357, 0x0105, "TP-Link Archer T1U"),
    (0x0E8D, 0x7630, "MediaTek MT7630U"),
    (0x0E8D, 0x7650, "MediaTek MT7650U"),
]

# ============================================================
# Endpoint addresses (mt76u_set_endpoints positional, verified by
# probe_hw.py descriptor dump on 0e8d:7610). Same layout as mt76x2u.
# ============================================================
EP_IN_PKT_RX        = 0x84   # in_ep[0]
EP_IN_CMD_RESP      = 0x85   # in_ep[1]
EP_OUT_INBAND_CMD   = 0x08   # out_ep[0]  <- FW upload + MCU
EP_OUT_AC_BE        = 0x04   # out_ep[1]
EP_OUT_AC_BK        = 0x05   # out_ep[2]
EP_OUT_AC_VI        = 0x06   # out_ep[3]
EP_OUT_AC_VO        = 0x07   # out_ep[4]
EP_OUT_HCCA         = 0x09   # out_ep[5]

# ============================================================
# Vendor bRequest codes — mt76 family shared.
# [SRC] data_dumps/mt76-source-v6.18/mt76x02_usb_core.c
# ============================================================
MT_VEND_DEV_MODE      = 0x01   # control: FW reset / IVB trigger via wValue
MT_VEND_MULTI_WRITE   = 0x06   # default-bus register write (4 bytes payload)
MT_VEND_MULTI_READ    = 0x07   # default-bus register read  (4 bytes payload)
MT_VEND_WRITE_FCE     = 0x42   # FCE single_wr (value in wValue, no payload)

# DEV_MODE wValue constants
MT_DEV_MODE_FW_RESET     = 0x0001
MT_DEV_MODE_IVB_TRIGGER  = 0x0012

# ============================================================
# Register addresses — [SRC] mt76x02_regs.h + mt76x02_mcu.h + mt76x0/mcu.h
# ============================================================
MT_CMB_CTRL                       = 0x0020   # [SRC] mt76x02_regs.h:14
MT_CMB_CTRL_XTAL_RDY              = 1 << 22  # BIT(22)
MT_CMB_CTRL_PLL_LD                = 1 << 23  # BIT(23)

MT_WLAN_FUN_CTRL                  = 0x0080   # chip on/off + reset
# MT_WLAN_FUN_CTRL bits — [SRC] mt76x02_regs.h:34-56
MT_WLAN_FUN_CTRL_WLAN_EN          = 1 << 0
MT_WLAN_FUN_CTRL_WLAN_CLK_EN      = 1 << 1
MT_WLAN_FUN_CTRL_WLAN_RESET_RF    = 1 << 2
MT_WLAN_FUN_CTRL_WLAN_RESET       = 1 << 3   # MT76x0 only (BIT(3) is CSR_F20M_CKEN on MT76x2)
MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL   = 1 << 5
MT_WLAN_FUN_CTRL_GPIO_OUT_EN      = 0xFF << 24   # GENMASK(31, 24)
MT_FCE_DMA_ADDR                   = 0x0230   # single_wr destination — chunk addr
MT_FCE_DMA_LEN                    = 0x0234   # single_wr destination — chunk len
MT_USB_DMA_CFG                    = 0x0238
MT_MCU_COM_REG0                   = 0x0730   # FW_READY = BIT(0)
MT_FCE_PSE_CTRL                   = 0x0800
MT_TX_CPU_FROM_FCE_BASE_PTR       = 0x09a0
MT_TX_CPU_FROM_FCE_MAX_COUNT      = 0x09a4
MT_TX_CPU_FROM_FCE_CPU_DESC_IDX   = 0x09a8
MT_FCE_PDMA_GLOBAL_CONF           = 0x09c4
MT_FCE_SKIP_FS                    = 0x0a6c
MT_MAC_CSR0                       = 0x1000   # ASIC version probe (used by wait_for_mac)
MT_MAC_SYS_CTRL                   = 0x1004   # [SRC] mt76x02_regs.h:269

# MT_MAC_SYS_CTRL bit fields. Kernel pre-FW writes 0x2c = ENABLE_TX | ENABLE_RX | BIT(5).
MT_MAC_SYS_CTRL_RESET_CSR         = 1 << 0
MT_MAC_SYS_CTRL_RESET_BBP         = 1 << 1
MT_MAC_SYS_CTRL_ENABLE_TX         = 1 << 2
MT_MAC_SYS_CTRL_ENABLE_RX         = 1 << 3
MT_MAC_SYS_CTRL_PRE_FW_VALUE      = 0x2c     # [SRC] mt76x0/usb_mcu.c:125

# (MT_CMB_CTRL above replaced the misnamed MT_PROBE_REG_0X20.)

# MT_USB_DMA_CFG bit fields — [SRC] mt76x02_regs.h:78-87
MT_USB_DMA_CFG_RX_BULK_AGG_TOUT_MASK = 0xFF  # GENMASK(7,0)
MT_USB_DMA_CFG_UDMA_TX_WL_DROP    = 1 << 16  # BIT(16)
MT_USB_DMA_CFG_RX_DROP_OR_PAD     = 1 << 18  # BIT(18)
MT_USB_DMA_CFG_RX_BULK_AGG_EN     = 1 << 21  # BIT(21)
MT_USB_DMA_CFG_RX_BULK_EN         = 1 << 22  # BIT(22)
MT_USB_DMA_CFG_TX_BULK_EN         = 1 << 23  # BIT(23)

# MT_MCU_COM_REG0 — FW running flag
MT_MCU_COM_REG0_FW_READY          = 1 << 0   # BIT(0)

# ============================================================
# MCU msg info-header fields (the 4 bytes prepended to each bulk-OUT chunk).
# [SRC] mt76x02_dma.h:33-46
# ============================================================
MT_MCU_MSG_LEN_MASK     = 0xFFFF        # GENMASK(15,0)
MT_MCU_MSG_PORT_SHIFT   = 27            # GENMASK(29,27)
MT_MCU_MSG_PORT_MASK    = 0x7 << 27
MT_MCU_MSG_TYPE_CMD     = 1 << 30       # BIT(30)
CPU_TX_PORT             = 2             # enum dma_msg_port — mt76x02_dma.h:43-51

# ============================================================
# FW upload constants — [SRC] mt76x0/mcu.h:14-15 + usb_mcu.c:13-14
# ============================================================
MT_MCU_IVB_SIZE              = 0x40          # bytes — first 0x40 of FW body is IVB
MT_MCU_DLM_OFFSET            = 0x80000       # DLM upload base address
MCU_FW_URB_MAX_PAYLOAD       = 0x38f8        # 14584 — total URB size cap
MCU_FW_CHUNK_DATA_MAX        = 14584 - 8     # 14576 — info(4)+pad(4) deducted

# mt76x02_fw_header structure (32 bytes). [SRC] mt76x02_mcu.h:71-78
#   __le32 ilm_len
#   __le32 dlm_len
#   __le16 build_ver
#   __le16 fw_ver
#   u8     pad[4]
#   char   build_time[16]
MT76X02_FW_HEADER_SIZE       = 32

# ============================================================
# Bring-up timing (from usb_mcu.c kernel comments).
# ============================================================
POST_FW_RESET_SLEEP_MS       = 6     # kernel usleep_range(5000, 6000)
INTER_CHUNK_SLEEP_MS         = 10    # kernel usleep_range(5000, 10000)
FW_READY_POLL_INTERVAL_MS    = 1     # kernel mt76_poll_msec interval
FW_READY_POLL_TIMEOUT_MS     = 1000  # kernel mt76_poll_msec timeout
