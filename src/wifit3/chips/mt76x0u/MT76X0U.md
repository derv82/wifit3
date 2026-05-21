# MT76x0U / MT7610U — Ground Truth

Verified facts only. Hypothesis-level material goes in commit messages, not
here. `[SRC]` = kernel source (`data_dumps/mt76-source-v6.18/`). `[WIRE]` =
pcap evidence from `usb_dumps/captures_mt76x0u/`.

## Identity

- **Family**: mt76, generation `mt76x02` (older sibling of mt76_connac, same
  family as mt76x2u). USB driver source lives in `mt76x0/usb.c` + `mt76x0/usb_mcu.c`,
  using `mt76x02_*.c` shared helpers. `[SRC]` `mt76x0/Makefile`.
- **Kernel module**: `mt76x0u` (vs `mt76x2u` for the WiFi-5 2T2R sibling).
- **VID:PID claimed**: 25 entries — see `constants.py::USB_IDS_MT76X0U`. The
  card on the dev machine is **`0e8d:7610`** (Sabrent NTWLAC / MediaTek
  MT7610U — also the silicon in the user's AWUS036ACM variant). `[SRC]`
  `mt76x0/usb.c:14-43`.
- **USB 2.0**, `bcdUSB=0x0201`, PyUSB speed=3 (HIGH 480 Mbps),
  `bMaxPacketSize0=64`. All bulk endpoints at maxPacketSize=512.
  `[WIRE]` `scripts/mt76x0u/probe_hw.py` output, 2026-05-21.

## Endpoint layout

After Zadig WinUSB binding, interface 0.0 exposes 2 bulk-IN + 6 bulk-OUT,
all at 512-byte maxPacketSize. Layout is **identical to mt76x2u** —
positional `mt76u_set_endpoints` assignment matches.

| Slot | Kernel name | Address |
|---|---|---|
| in_ep[0]  | `MT_EP_IN_PKT_RX`      | **0x84** |
| in_ep[1]  | `MT_EP_IN_CMD_RESP`    | **0x85** |
| out_ep[0] | `MT_EP_OUT_INBAND_CMD` | **0x08** |  ← FW upload + MCU |
| out_ep[1] | `MT_EP_OUT_AC_BE`      | 0x04 |
| out_ep[2] | `MT_EP_OUT_AC_BK`      | 0x05 |
| out_ep[3] | `MT_EP_OUT_AC_VI`      | 0x06 |
| out_ep[4] | `MT_EP_OUT_AC_VO`      | 0x07 |
| out_ep[5] | `MT_EP_OUT_HCCA`       | 0x09 |

`[WIRE]` probe_hw.py descriptor dump.

## Cold-boot vs warm-boot in captures

Of the three `usb_dumps/captures_mt76x0u/capture-N.pcap` files, only **capture-2
and capture-3 are cold boots** (FW upload visible). **capture-1 is warm** —
chip retained FW from a prior Kali session, so kernel skipped upload.
Detection method: `scripts/mt76x0u/find_fw_window.py` counts FW-reset and
IVB-trigger frames per capture:

- capture-1: 0 FW resets, 0 IVB triggers → warm
- capture-2: FW reset at frame 289, IVB at 393 → cold
- capture-3: FW reset at frame 291, IVB at 397 → cold

`main.log` files all say "INSERT NOW" at T=0 but that's the user prompt, not
the chip's actual state. Trust pcap content over `main.log` headers.

## Register-access protocol

Same encoding as mt76x2u (shared `mt76x02` family):

| Bus | Address marker | Read bReq | Write bReq |
|---|---|---|---|
| Default (MAC/BB/RF) | none | `0x07 MT_VEND_MULTI_READ` | `0x06 MT_VEND_MULTI_WRITE` |
| FCE (chunks)        | (see below) | n/a | `0x42 MT_VEND_WRITE_FCE` |

For default reads/writes: `wValue = addr >> 16`, `wIndex = addr & 0xFFFF`,
payload = 4-byte little-endian value.

`MT_VEND_WRITE_FCE` is a "single_wr" — no payload; the 16-bit value is
packed into `wValue` and the 16-bit destination offset into `wIndex`. Used
to set DMA_ADDR and DMA_LEN registers in two 16-bit halves each (4 control
transfers per FW chunk before the bulk-OUT). `[SRC]` `usb.c:215` (`mt76u_single_wr`).

## Firmware

- **Linux-firmware variants**: `mt7610u.bin` (80,288 B) and `mt7610e.bin`
  (80,680 B). Kernel tries `mt7610e` first then falls back to `mt7610u`.
  `[SRC]` `usb_mcu.c:67-83` (`mt76x0_get_firmware`).
- **What Kali actually uploads**: `mt7610e.bin`. The pcap-extracted upload
  body matches `mt7610e.bin[32:]` byte-for-byte (and does NOT match
  `mt7610u.bin[32:]`). `[WIRE]` `scripts/mt76x0u/extract_mt7610u_fw.py`.
- **File layout**: `[32-byte mt76x02_fw_header][IVB (first 0x40 of body)][ILM remainder][DLM]`
  `[SRC]` `mt76x02_mcu.h:71-78`.
- **mt7610e header**: ilm_len=69172, dlm_len=11476. Total body = 80,648 B
  = 64 (IVB) + 69108 (ILM remainder) + 11476 (DLM).
- **Single-stage** — unlike mt76x2u's two-stage (ROM patch + main FW),
  mt76x0u uploads ONE blob. `[SRC]` `mt76x0/usb_mcu.c:85-162`
  (`mt76x0u_load_firmware`).

## M1 — wire-confirmed FW upload sequence

From `capture-2.pcap`, frames 269-399. Citations are `[SRC]` `mt76x0/usb_mcu.c`
line numbers and `[WIRE]` capture-2 frame numbers.

1. **Chip on + reset**: write `MT_WLAN_FUN_CTRL=0xff000002` then `=0xff000003`.
   `[SRC]` `mt76x0/init.c:mt76x0_chip_onoff`. `[WIRE]` f271, f273.
2. **Wait for MAC**: read 0x0020 + 0x1000 (`MAC_CSR0`). `[WIRE]` f275, f277.
3. **Initial DMA cfg**: `MT_USB_DMA_CFG = 0x00c00000` (RX_BULK_EN | TX_BULK_EN).
   `[SRC]` usb_mcu.c:92. `[WIRE]` f279.
4. **FW-running check**: read `MT_MCU_COM_REG0` (BIT(0) == 1 means running).
   `[SRC]` `mt76x0_firmware_running`. `[WIRE]` f281.
5. **Magic write**: `0x1004 = 0x2c`. `[SRC]` usb_mcu.c:125. `[WIRE]` f283.
6. **DMA cfg with AGG_TOUT**: read MT_USB_DMA_CFG, OR in `FIELD_PREP(RX_BULK_AGG_TOUT, 0x20)`,
   write back → 0x00c00020. `[SRC]` usb_mcu.c:127. `[WIRE]` f285, f287.
7. **FW reset**: vendor write `DEV_MODE wValue=0x0001, wLen=0`.
   `[SRC]` `mt76x02u_mcu_fw_reset`. `[WIRE]` f289.
8. **Sleep 5–6 ms**. `[SRC]` usb_mcu.c:131.
9. **PSE_CTRL = 1 (twice — WIRE DEVIATION)**. Kernel source only writes once
   at usb_mcu.c:133, but WIRE shows two back-to-back identical writes. Port
   the duplicate verbatim. `[WIRE]` f291, f293.
10. **FCE config**: `BASE_PTR=0x400230, MAX_COUNT=1, PDMA_GLOBAL_CONF=0x44,
    SKIP_FS=3`. `[SRC]` usb_mcu.c:136-142. `[WIRE]` f295-301.
11. **UDMA_TX_WL_DROP toggle**: read DMA_CFG, set BIT(16), write
    (0x00c10020), clear BIT(16), write (0x00c00020). `[SRC]` usb_mcu.c:144-148.
    `[WIRE]` f303-307.
12. **Upload 6 chunks** (5 ILM + 1 DLM). For each chunk: 4× `MT_VEND_WRITE_FCE`
    (DMA_ADDR lo/hi at 0x0230/0x0232 + DMA_LEN lo/hi at 0x0234/0x0236), then
    bulk-OUT on EP 0x08 = `[4B info: PORT|LEN|TYPE_CMD][chunk bytes][4B zero pad]`,
    then RMW increment of `MT_TX_CPU_FROM_FCE_CPU_DESC_IDX`. Inter-chunk
    sleep 5–10 ms. `[SRC]` `mt76x02u_mcu_fw_send_data` + `__mt76x02u_mcu_fw_send_data`.
    `[WIRE]` f309-391.
    - **Chunk size**: `MCU_FW_URB_MAX_PAYLOAD = 0x38f8 = 14584` total URB,
      `max_len = 14584 - 8 = 14576` chunk data bytes. `[SRC]` usb_mcu.c:13-14.
    - **info field encoding**: `(PORT=CPU_TX_PORT=2 << 27) | (len << 0) | BIT(30)
      = 0x500038f0` for a 14576-byte chunk. `[WIRE]` f317 info=0x500038f0.
    - **Chunk destinations** (cumulative across ILM, then jumps to DLM):
      0x40 → 0x3930 → 0x7220 → 0xab10 → 0xe400 (last ILM, 10804 B) → 0x80000 (DLM, 11476 B).
13. **IVB trigger**: vendor write `DEV_MODE wValue=0x0012, wLen=0x40` carrying
    the 64-byte IVB body (first 0x40 of the FW payload). `[SRC]` usb_mcu.c:47-49.
    `[WIRE]` f393, payload starts `480000784800001e4800001c4800001a...`.
14. **Poll FW_READY**: read `MT_MCU_COM_REG0`, wait for `BIT(0)==1`, 1 ms
    interval, 1000 ms timeout. `[SRC]` usb_mcu.c:53. `[WIRE]` f395, f397, f405.
    Live FW comes ready within ~3 polls.
15. **Post-upload PSE_CTRL = 1**. `[SRC]` usb_mcu.c:154. `[WIRE]` f399.

## Constants verified by grep

| Symbol | Value | Source |
|---|---|---|
| `MT_FCE_DMA_ADDR` | 0x0230 | mt76x02_regs.h:159 |
| `MT_FCE_DMA_LEN` | 0x0234 | mt76x02_regs.h:160 |
| `MT_USB_DMA_CFG` | 0x0238 | mt76x02_regs.h:161 |
| `MT_MCU_COM_REG0` | 0x0730 | mt76x02_mcu.h:13 |
| `MT_FCE_PSE_CTRL` | 0x0800 | mt76x02_regs.h:242 |
| `MT_TX_CPU_FROM_FCE_BASE_PTR` | 0x09a0 | mt76x02_regs.h:259 |
| `MT_TX_CPU_FROM_FCE_MAX_COUNT` | 0x09a4 | mt76x02_regs.h:260 |
| `MT_TX_CPU_FROM_FCE_CPU_DESC_IDX` | 0x09a8 | mt76x02_regs.h:261 |
| `MT_FCE_PDMA_GLOBAL_CONF` | 0x09c4 | mt76x02_regs.h:262 |
| `MT_FCE_SKIP_FS` | 0x0a6c | mt76x02_regs.h:263 |
| `MT_WLAN_FUN_CTRL` | 0x0080 | mt76x02_regs.h:33 |
| `MT_MCU_IVB_SIZE` | 0x40 | mt76x0/mcu.h:14 |
| `MT_MCU_DLM_OFFSET` | 0x80000 | mt76x0/mcu.h:15 |
| `MT_USB_DMA_CFG_RX_BULK_AGG_TOUT` | GENMASK(7,0) | mt76x02_regs.h:78 |
| `MT_USB_DMA_CFG_UDMA_TX_WL_DROP` | BIT(16) | mt76x02_regs.h:80 |
| `MT_USB_DMA_CFG_RX_BULK_AGG_EN` | BIT(21) | mt76x02_regs.h:85 |
| `MT_USB_DMA_CFG_RX_BULK_EN` | BIT(22) | mt76x02_regs.h:86 |
| `MT_USB_DMA_CFG_TX_BULK_EN` | BIT(23) | mt76x02_regs.h:87 |
| `MT_MCU_MSG_LEN` | GENMASK(15,0) | mt76x02_dma.h:33 |
| `MT_MCU_MSG_PORT` | GENMASK(29,27) | mt76x02_dma.h:36 |
| `MT_MCU_MSG_TYPE_CMD` | BIT(30) | mt76x02_dma.h:38 |
| `CPU_TX_PORT` (enum) | 2 | mt76x02_dma.h:43-51 |
| `MT_VEND_DEV_MODE` | 0x01 | (shared mt76 vendor reqs) |
| `MT_VEND_MULTI_WRITE` | 0x06 | mt76x02_usb_core.c |
| `MT_VEND_MULTI_READ` | 0x07 | mt76x02_usb_core.c |
| `MT_VEND_WRITE_FCE` | 0x42 | mt76x02_usb_core.c |
| `MCU_FW_URB_MAX_PAYLOAD` | 0x38f8 = 14584 | mt76x0/usb_mcu.c:13 |

## Open questions / hypothesis-level

- The duplicate `PSE_CTRL=1` write at f291+f293 doesn't appear in kernel
  source. Possible explanations: (a) a barrier/repost done by the `mt76_wr`
  macro under certain build configs, (b) Kali running a slightly different
  driver version, (c) a side-effect of the kernel's reset path that gets
  retried. We port the duplicate to stay wire-faithful; if FW_READY ack
  fails without the duplicate, the duplicate was load-bearing.
