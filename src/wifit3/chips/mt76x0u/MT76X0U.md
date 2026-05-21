# MT76x0U / MT7610U — Ground Truth

Verified facts only. Anything unverified is excluded; if a claim isn't
backed by either kernel source or pcap evidence, it isn't here.

- `[SRC] <file>:<line>` — kernel source under `data_dumps/mt76-source-v6.18/`.
- `[WIRE] capture-N:fN` — frame N in `usb_dumps/captures_mt76x0u/capture-N.pcap`.
- `[HW]` — observed on the dev card 0e8d:7610.

## Identity

- **Family**: mt76, generation `mt76x02` (older sibling of mt76_connac).
  Shares `mt76x02_*.c` helpers with mt76x2u. [SRC] mt76x0/Makefile.
- **Kernel module**: `mt76x0u`. Sources: `mt76x0/usb.c`, `mt76x0/usb_mcu.c`,
  `mt76x0/init.c`, `mt76x0/eeprom.c`, `mt76x0/phy.c`, plus shared
  `mt76x02_eeprom.c`, `mt76x02_mcu.c`, `mt76x02_usb_core.c`, `mt76x02_usb_mcu.c`.
- **VID:PID claimed**: 25 entries — see `constants.py::USB_IDS_MT76X0U`.
  Dev card is `0e8d:7610` (Sabrent NTWLAC / MediaTek MT7610U).
  [SRC] mt76x0/usb.c:14-43, [WIRE] probe_hw.py output.
- **Silicon**: MAC_CSR0 reads `0x76502000` on the dev card. The high 16 bits
  identify as `0x7650` (MT7650 family chip behind a 7610 USB descriptor —
  same kernel driver per id_table). [HW]
- **USB 2.0**: `bcdUSB=0x0201`, PyUSB speed=3 (HIGH 480 Mbps),
  `bMaxPacketSize0=64`. All bulk endpoints maxPacketSize=512. [WIRE] probe_hw.py.

## Endpoint layout

After Zadig WinUSB binding, interface 0.0 exposes 2 bulk-IN + 6 bulk-OUT.
Positional `mt76u_set_endpoints` slot assignment matches the mt76x2u sibling.

| Slot | Kernel name | Address |
|---|---|---|
| in_ep[0]  | `MT_EP_IN_PKT_RX`      | **0x84** |
| in_ep[1]  | `MT_EP_IN_CMD_RESP`    | **0x85**  ← MCU responses |
| out_ep[0] | `MT_EP_OUT_INBAND_CMD` | **0x08**  ← FW upload + MCU commands |
| out_ep[1] | `MT_EP_OUT_AC_BE`      | 0x04 |
| out_ep[2] | `MT_EP_OUT_AC_BK`      | 0x05 |
| out_ep[3] | `MT_EP_OUT_AC_VI`      | 0x06 |
| out_ep[4] | `MT_EP_OUT_AC_VO`      | 0x07 |
| out_ep[5] | `MT_EP_OUT_HCCA`       | 0x09 |

[WIRE] probe_hw.py descriptor dump.

## Capture inventory

Three pcaps under `usb_dumps/captures_mt76x0u/`. The capture script's
`main.log` always says "INSERT NOW" at T=0, but that's the user prompt — it
doesn't reflect chip state. Trust pcap content via
`scripts/mt76x0u/find_fw_window.py`, which counts FW-reset (DEV_MODE wVal=1)
and IVB-trigger (DEV_MODE wVal=0x12) frames:

| capture | FW resets | IVB triggers | Verdict |
|---|---|---|---|
| capture-1 | 0 | 0 | warm boot — chip retained FW from a prior session |
| capture-2 | 1 (f289) | 1 (f393) | cold boot — FW upload visible |
| capture-3 | 1 (f291) | 1 (f397) | cold boot — FW upload visible |

The byte-verified FW extraction (`extract_mt7610u_fw.py`) runs against
capture-2.

## Vendor request protocol

The kernel `__mt76u_vendor_request` ([SRC] usb.c:18-44) sends control
transfers with retry semantics:

- **bRequest** values used in M1+M2:
  - `0x01 MT_VEND_DEV_MODE` — FW reset (wVal=1), IVB trigger (wVal=0x12)
  - `0x06 MT_VEND_MULTI_WRITE` — register write, 4-byte LE payload
  - `0x07 MT_VEND_MULTI_READ` — register read, 4-byte LE response
  - `0x42 MT_VEND_WRITE_FCE` — single_wr (value in wValue, no payload)
- **Default-bus addr encoding**: `wValue = addr >> 16`, `wIndex = addr & 0xFFFF`.
- **Retry**: up to 10 attempts, 300 ms timeout each, 5–10 ms backoff between.
  Retries on timeout; gives up on `-ENODEV` / `-EPROTO`.
  [SRC] usb.c:30-40 (`MT_VEND_REQ_MAX_RETRY=10`, `MT_VEND_REQ_TOUT_MS=300`).
- **`MT_VEND_WRITE_FCE` single_wr**: split a 32-bit value into two control
  transfers (low half wValue at wIndex=reg, high half at wIndex=reg+2,
  NO payload either). Used for the FW-chunk DMA_ADDR/DMA_LEN writes.
  [SRC] usb.c:215 (`mt76u_single_wr`).

## Firmware

- **Linux-firmware variants on disk**: `mt7610u.bin` (80,288 B) and
  `mt7610e.bin` (80,680 B). Kernel tries `mt7610e` first, falls back to
  `mt7610u`. [SRC] mt76x0/usb_mcu.c:67-83.
- **What Kali actually uploaded** on the dev card: `mt7610e.bin`. The
  pcap-extracted body matches `mt7610e.bin[32:]` byte-for-byte and does
  NOT match `mt7610u.bin[32:]`. [WIRE] extract_mt7610u_fw.py on capture-2.
- **File layout** (32-byte `mt76x02_fw_header` + body):
  `[ilm_len u32][dlm_len u32][build_ver u16][fw_ver u16][pad 4B][build_time 16B][IVB 64B][ILM remainder][DLM]`.
  [SRC] mt76x02_mcu.h:71-78.
- **mt7610e blob**: ilm_len=69172, dlm_len=11476, fw_ver=0.1.00,
  build=0x7640, build_time=`201404011018____`. Body = 64 (IVB) + 69108
  (ILM remainder) + 11476 (DLM) = 80,648 bytes. [HW]
- **Single-stage upload** — unlike mt76x2u's two-stage (ROM patch + main FW),
  mt76x0u uploads one blob. [SRC] mt76x0/usb_mcu.c:85-162 (`mt76x0u_load_firmware`).

## M1 — wire-confirmed FW upload sequence

From capture-2.pcap, frames 269-399. Citations are [SRC] mt76x0/usb_mcu.c
line numbers and [WIRE] capture-2 frame numbers.

The host driver path: `mt76x0u_probe` → `usb_reset_device` →
`mt76u_init` → `mt76x0_chip_onoff(false, false)` → `wait_for_mac` →
`mt76x0u_register_device` → `mt76x0u_init_hardware`:
- `mt76x0_chip_onoff(true, reset)` — chip on with reset
- `mt76x02_wait_for_mac`
- `mt76x0u_mcu_init` → `mt76x0u_load_firmware` (this is M1's main work)

Within `mt76x0u_load_firmware` (the FW-upload-window WIRE captures):

1. **`mt76x0_chip_onoff(true, true)`** — full port at firmware.py:`chip_onoff`.
   Branches on initial `MT_WLAN_FUN_CTRL`: if `WLAN_EN` was set, pulses
   `WLAN_RESET | WLAN_RESET_RF` then clears. Always then writes val with
   `GPIO_OUT_EN` set + `FRC_WL_ANT_SEL` cleared. Then `set_wlan_state` writes
   val with `WLAN_EN | WLAN_CLK_EN` and polls `MT_CMB_CTRL` for
   `XTAL_RDY | PLL_LD` (BIT 22 | BIT 23). [SRC] mt76x0/init.c:16-69.
   - On the dev card the initial state was `WLAN_EN=0`, so the observed
     wire sequence was just 2 writes (`MT_WLAN_FUN_CTRL=0xff000002` then
     `=0xff000003`) followed by one `MT_CMB_CTRL` read. [WIRE] f269-275.
2. **`mt76x02_wait_for_mac`** — poll `MT_MAC_CSR0` (0x1000) until value
   not `0` and not `~0`, max 500 iterations × ~7.5 ms.
   [SRC] mt76x02_mac.h:149-168. [WIRE] f277.
3. **Initial DMA cfg**: `MT_USB_DMA_CFG = RX_BULK_EN | TX_BULK_EN`
   (= 0x00c00000). [SRC] usb_mcu.c:92. [WIRE] f279.
4. **`mt76x0_firmware_running` check**: read `MT_MCU_COM_REG0` (0x0730),
   check `BIT(0)`. If set, FW already running — return early.
   [SRC] usb_mcu.c:95. [WIRE] f281.
5. **`MT_MAC_SYS_CTRL = 0x2c`** — kernel writes 0x2c which is
   `ENABLE_TX | ENABLE_RX | BIT(5)`. [SRC] usb_mcu.c:125, mt76x02_regs.h:269-273.
   [WIRE] f283.
6. **DMA cfg with AGG_TOUT**: read `MT_USB_DMA_CFG`, OR in
   `RX_BULK_EN | TX_BULK_EN | (AGG_TOUT=0x20)`, write back → 0x00c00020.
   [SRC] usb_mcu.c:127 (`mt76_set`). [WIRE] f285, f287.
7. **FW reset**: `MT_VEND_DEV_MODE` wValue=0x0001, wLen=0.
   [SRC] mt76x02_usb_mcu.c:207. [WIRE] f289.
8. **Sleep 5–6 ms**. [SRC] usb_mcu.c:131 (`usleep_range(5000, 6000)`).
9. **`MT_FCE_PSE_CTRL = 1` written TWICE in succession**. Kernel source
   only emits the single write at usb_mcu.c:133, but capture-2 shows two
   identical writes back-to-back. We port both verbatim. Whether the
   duplicate is load-bearing is untested — code path that omits it has
   never been run on the dev card. [WIRE] f291, f293.
10. **FCE config** — 4 writes: `MT_TX_CPU_FROM_FCE_BASE_PTR = 0x400230`,
    `MT_TX_CPU_FROM_FCE_MAX_COUNT = 1`, `MT_FCE_PDMA_GLOBAL_CONF = 0x44`,
    `MT_FCE_SKIP_FS = 3`. [SRC] usb_mcu.c:136-142. [WIRE] f295-301.
11. **`UDMA_TX_WL_DROP` toggle**: read `MT_USB_DMA_CFG`, set BIT(16),
    write (0x00c10020), clear BIT(16), write (0x00c00020).
    [SRC] usb_mcu.c:144-148. [WIRE] f303-307.
12. **Upload 6 chunks (5 ILM + 1 DLM)**. Each chunk:
    - 4× `MT_VEND_WRITE_FCE` single_wr to set `DMA_ADDR` (low at 0x0230,
      high at 0x0232) and `DMA_LEN` (low at 0x0234, high at 0x0236).
    - Bulk-OUT on EP 0x08: `[4B info][chunk bytes][4B zero pad]`.
    - RMW increment of `MT_TX_CPU_FROM_FCE_CPU_DESC_IDX`.
    - Sleep 5–10 ms before next chunk.
    [SRC] mt76x02_usb_mcu.c:215-251 (`__mt76x02u_mcu_fw_send_data`).
    [WIRE] f309-391.
    - **Chunk size**: `MCU_FW_URB_MAX_PAYLOAD = 0x38f8 = 14584` total URB,
      `chunk_data_max = 14584 - 8 = 14576`. [SRC] usb_mcu.c:13-14.
    - **FW-chunk info field** (different from MCU-msg info; no CMD_TYPE/SEQ):
      `MT_MCU_MSG_TYPE_CMD | FIELD_PREP(MT_MCU_MSG_PORT, CPU_TX_PORT) |
      FIELD_PREP(MT_MCU_MSG_LEN, len)` = 0x500038f0 for a 14576-byte chunk.
      [SRC] mt76x02_usb_mcu.c:223-225. [WIRE] f317 info=0x500038f0.
    - **Chunk destinations**: 0x40 → 0x3930 → 0x7220 → 0xab10 → 0xe400
      (last ILM chunk, 10804 B) → 0x80000 (DLM, 11476 B). [WIRE] f311-381.
13. **IVB trigger**: `MT_VEND_DEV_MODE` wValue=0x0012, wLen=0x40, data =
    first 64 bytes of FW body (the IVB section). [SRC] usb_mcu.c:47-49.
    [WIRE] f393.
14. **Poll FW_READY**: read `MT_MCU_COM_REG0`, wait for `BIT(0)==1`, 1 ms
    poll interval, 1000 ms timeout. [SRC] usb_mcu.c:53. [WIRE] f395, f397.
    - On the dev card: the first read after IVB-trigger times out at 300 ms
      and is retried by `__mt76u_vendor_request`; the second read returns
      `BIT(0)==1`. End-to-end M1 takes ~700 ms. [HW]
15. **Post-upload `MT_FCE_PSE_CTRL = 1`**. [SRC] usb_mcu.c:154. [WIRE] f399.

## Post-FW init (M2 prerequisites — runs in `driver.connect()` after FW_READY)

After `mt76x0u_mcu_init` returns, `mt76x0u_init_hardware` continues with
[SRC] mt76x0/usb.c:151-177:

16. **`mt76x0_init_usb_dma`** — [SRC] mt76x0/usb.c:46-71. [WIRE] f401-411.
    - Read `MT_USB_DMA_CFG`, set `RX_BULK_EN | TX_BULK_EN`, clear
      `RX_BULK_AGG_EN`, write back.
    - Read `MT_MCU_COM_REG0` to verify MCU is ready (kernel only warns
      if the bit is clear; doesn't fail).
    - `RX_DROP_OR_PAD` toggle: read DMA cfg, set BIT(18), write, clear
      BIT(18), write. [WIRE] f409, f411.
17. **`mt76x0_reset_csr_bbp`** — write `MT_MAC_SYS_CTRL = RESET_CSR | RESET_BBP`
    (= 0x3), `msleep(200)`, then RMW-clear those bits.
    [SRC] mt76x0/init.c:72-81. [WIRE] f417 (write 0x3), f419 (read), f421
    (write 0x0).

Without (16) and (17), MCU bulk-IN reads on EP 0x85 time out — the chip's
RX-DMA path isn't armed. Verified on the dev card: skipping these steps
caused the M2 smoke test to fail with 5×300 ms timeouts. [HW]

## M2 — MCU command channel

Once post-FW init is done, the kernel uses an in-band MCU command channel
for most subsequent operations:

- **Bulk-OUT on EP 0x08**: `[4B info][payload, padded to 4-aligned][4B zero tail]`.
- **Bulk-IN on EP 0x85**: `[4B rxfce][response payload, padded][4B zero tail]`,
  up to `MCU_RESP_URB_SIZE = 1024` bytes total. [SRC] mt76.h:661.

### Info-header encoding (host → chip)

Built by `__mt76x02u_mcu_send_msg` + `mt76x02u_skb_dma_info`
([SRC] mt76x02_usb_mcu.c:69-107, mt76x02_usb_core.c:46-62):

| Bits | Field | Value |
|---|---|---|
| 30 | `MT_MCU_MSG_TYPE_CMD` | 1 (always for cmd msgs) |
| 29-27 | `MT_MCU_MSG_PORT` | `CPU_TX_PORT = 2` |
| 26-20 | `MT_MCU_MSG_CMD_TYPE` | command code (e.g. `CMD_FUN_SET_OP=1`, `CMD_RANDOM_READ=10`, `CMD_RANDOM_WRITE=12`) |
| 19-16 | `MT_MCU_MSG_CMD_SEQ` | 4-bit sequence id (1..15; 0 means "no response expected") |
| 15-0 | `MT_MCU_MSG_LEN` | `round_up(payload_len, 4)` |

The trailing pad on the bulk-OUT is `round_up(payload_len, 4) - payload_len + 4`
bytes (zeros), so the bulk-OUT total = 4 + aligned_len + (alignment + 4).
[SRC] mt76x02_usb_core.c:60.

### Sequence-id management

Kernel `msg_seq` is a host-side counter that pre-increments and masks to
4 bits, skipping zero (so seq values are always 1..15). Only commands with
`wait_resp=true` consume a non-zero seq; `wait_resp=false` commands use
seq=0. [SRC] mt76x02_usb_mcu.c:82-86.

### Response (chip → host)

Format `[4B rxfce][N×8B (addr, value) pairs][4B zero tail]` — but the
payload meaning depends on the command:

| Bits | Field |
|---|---|
| 23-20 | `MT_RX_FCE_INFO_EVT_TYPE` (0 = `EVT_CMD_DONE`) |
| 19-16 | `MT_RX_FCE_INFO_CMD_SEQ` (echoes request seq) |

[SRC] mt76x02_dma.h:25-26, dma.h:150-158.

- `CMD_RANDOM_READ` response: payload is `[base+reg, value]` pairs;
  the kernel checks `(addr - base) == requested_reg`. [SRC] mt76x02_usb_mcu.c:20-35.
- `CMD_RANDOM_WRITE` / `CMD_FUN_SET_OP` responses: payload is unused
  filler; only the rxfce header (seq + EVT_CMD_DONE) is meaningful.

### Wait/retry semantics

[SRC] mt76x02_usb_mcu.c:37-67 (`mt76x02u_mcu_wait_resp`):
- Up to 5 read attempts, 300 ms timeout each.
- Retries on timeout; bails on any other error.
- On each successful read, checks `seq == FIELD_GET(CMD_SEQ, rxfce) &&
  EVT_TYPE == EVT_CMD_DONE`. If not, logs an error and continues the loop.

### Register access via MCU — base address

Kernel-side MCU register access goes through `mt76_mcu_{wr,rd}_rp(dev, base, ...)`
where the wire address is `base + reg`. All callers in mt76x0 pass
`base = MT_MCU_MEMMAP_WLAN = 0x410000`. So `MT_MAC_CSR0` (0x1000) on the
wire is `0x00411000`. [SRC] mt76x02_mcu.h:19, mt76x0/init.c:84 (RANDOM_WRITE
macro). [WIRE] f427 payload, every addr is `0x00411xxx`.

### First MCU command after MAC reset — `Q_SELECT`

Kernel `mt76x0_init_hardware` line 183 calls
`mt76x02_mcu_function_select(dev, Q_SELECT, 1)` before any other MCU
operation. This is `CMD_FUN_SET_OP` (cmd=1) with payload `<id=Q_SELECT=1,
value=1>`, sent with `wait=false`. Without it, subsequent MCU commands
time out. [SRC] mt76x0/init.c:183, mt76x02_mcu.c:82-99. [WIRE] capture-2:f423
payload `01000000 01000000`. [HW] confirmed: MCU CMD_RANDOM_READ times out
if sent before Q_SELECT.

## M2 — EFUSE reads

The on-die EFUSE is read via the `MT_EFUSE_CTRL` (0x0024) protocol, NOT
via `MT_VEND_READ_EEPROM` (that bRequest is for an external EEPROM, which
mt76x0u silicon doesn't have).

### Protocol (`mt76x02_efuse_read`, [SRC] mt76x02_eeprom.c:11-43)

For each 16-byte block read:
1. Read `MT_EFUSE_CTRL`.
2. Clear AIN (bits 16-25) + MODE (bits 6-7); set AIN = `addr & ~0xf`,
   MODE = `MT_EE_READ (0)` or `MT_EE_PHYSICAL_READ (1)`, KICK = BIT(30).
3. Write `MT_EFUSE_CTRL`.
4. Poll `MT_EFUSE_CTRL` until KICK clears, max 1000 ms.
5. `udelay(2)`.
6. Re-read `MT_EFUSE_CTRL`; if AOUT (bits 0-5) == 0x3F, the block is
   unburned — return `0xff × 16`.
7. Otherwise read 4× `MT_EFUSE_DATA(0..3)` (0x0028, 0x002C, 0x0030, 0x0034)
   to get the 16 bytes.

`mt76x02_get_efuse_data(base, len)` repeats step 1-7 for each 16-byte block
in `[base, base+len)`. Length must be a positive multiple of 16.
[SRC] mt76x02_eeprom.c:57-69.

### EFUSE field offsets

[SRC] mt76x02_eeprom.h:14-95 (`enum mt76x02_eeprom_field`):

| Offset | Field | Notes |
|---|---|---|
| 0x004 | `MT_EE_MAC_ADDR` | 6 bytes |
| 0x034 | `MT_EE_NIC_CONF_0` | u16 — RX_PATH(3:0), TX_PATH(7:4), PA_TYPE(9:8), BOARD_TYPE(13:12) |
| 0x036 | `MT_EE_NIC_CONF_1` | u16 — HW_RF_CTRL(0), TX_ALC_EN(13), etc. |
| 0x03A | `MT_EE_FREQ_OFFSET` | u8 — signed offset; 0xFF means unburned/use default |
| 0x1E0 | `MT_EE_USAGE_MAP_START` | start of usage-map region (size check only) |

`BOARD_TYPE` values: 1 = 2GHz-only, 2 = 5GHz-only, others = dual-band.
[SRC] mt76x02_eeprom.c:72-89 (`mt76x02_eeprom_parse_hw_cap`).

### Observed on the dev card

[HW] EFUSE summary read at 2026-05-21:
- `MAC_ADDR`: present (not all 0xFF) — used as the interface MAC.
- `NIC_CONF_0 = 0xfd11` → RX_PATH=1, TX_PATH=1, BOARD_TYPE=3 (dual-band).
- `NIC_CONF_1 = 0x1008`.
- `FREQ_OFFSET = 22` (signed).
- Block at offset 0x010 reports unburned (`0xFF × 16`) — handled correctly.

## Constants — verified by grep

All values used in M1+M2 with their kernel source line.

| Symbol | Value | Source |
|---|---|---|
| `MT_CMB_CTRL` | 0x0020 | mt76x02_regs.h:14 |
| `MT_CMB_CTRL_XTAL_RDY` | BIT(22) | mt76x02_regs.h:15 |
| `MT_CMB_CTRL_PLL_LD` | BIT(23) | mt76x02_regs.h:16 |
| `MT_EFUSE_CTRL` | 0x0024 | mt76x02_regs.h:18 |
| `MT_EFUSE_CTRL_AOUT` | GENMASK(5,0) | mt76x02_regs.h:19 |
| `MT_EFUSE_CTRL_MODE` | GENMASK(7,6) | mt76x02_regs.h:20 |
| `MT_EFUSE_CTRL_AIN` | GENMASK(25,16) | mt76x02_regs.h:23 |
| `MT_EFUSE_CTRL_KICK` | BIT(30) | mt76x02_regs.h:24 |
| `MT_EFUSE_DATA_BASE` | 0x0028 | mt76x02_regs.h:27 |
| `MT_WLAN_FUN_CTRL` | 0x0080 | mt76x02_regs.h:33 |
| `MT_WLAN_FUN_CTRL_WLAN_EN` | BIT(0) | mt76x02_regs.h:34 |
| `MT_WLAN_FUN_CTRL_WLAN_CLK_EN` | BIT(1) | mt76x02_regs.h:35 |
| `MT_WLAN_FUN_CTRL_WLAN_RESET_RF` | BIT(2) | mt76x02_regs.h:36 |
| `MT_WLAN_FUN_CTRL_WLAN_RESET` | BIT(3) /* MT76x0 */ | mt76x02_regs.h:43 |
| `MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL` | BIT(5) | mt76x02_regs.h:47 |
| `MT_WLAN_FUN_CTRL_GPIO_OUT_EN` | GENMASK(31,24) | mt76x02_regs.h:56 |
| `MT_FCE_DMA_ADDR` | 0x0230 | mt76x02_regs.h:159 |
| `MT_FCE_DMA_LEN` | 0x0234 | mt76x02_regs.h:160 |
| `MT_USB_DMA_CFG` | 0x0238 | mt76x02_regs.h:161 |
| `MT_USB_DMA_CFG_RX_BULK_AGG_TOUT` | GENMASK(7,0) | mt76x02_regs.h:78 |
| `MT_USB_DMA_CFG_UDMA_TX_WL_DROP` | BIT(16) | mt76x02_regs.h:80 |
| `MT_USB_DMA_CFG_RX_DROP_OR_PAD` | BIT(18) | mt76x02_regs.h:82 |
| `MT_USB_DMA_CFG_RX_BULK_AGG_EN` | BIT(21) | mt76x02_regs.h:85 |
| `MT_USB_DMA_CFG_RX_BULK_EN` | BIT(22) | mt76x02_regs.h:86 |
| `MT_USB_DMA_CFG_TX_BULK_EN` | BIT(23) | mt76x02_regs.h:87 |
| `MT_MCU_COM_REG0` | 0x0730 | mt76x02_mcu.h:13 |
| `MT_FCE_PSE_CTRL` | 0x0800 | mt76x02_regs.h:242 |
| `MT_TX_CPU_FROM_FCE_BASE_PTR` | 0x09a0 | mt76x02_regs.h:259 |
| `MT_TX_CPU_FROM_FCE_MAX_COUNT` | 0x09a4 | mt76x02_regs.h:260 |
| `MT_TX_CPU_FROM_FCE_CPU_DESC_IDX` | 0x09a8 | mt76x02_regs.h:261 |
| `MT_FCE_PDMA_GLOBAL_CONF` | 0x09c4 | mt76x02_regs.h:262 |
| `MT_FCE_SKIP_FS` | 0x0a6c | mt76x02_regs.h:263 |
| `MT_MAC_CSR0` | 0x1000 | mt76x02_regs.h:267 |
| `MT_MAC_SYS_CTRL` | 0x1004 | mt76x02_regs.h:269 |
| `MT_MAC_SYS_CTRL_RESET_CSR` | BIT(0) | mt76x02_regs.h:270 |
| `MT_MAC_SYS_CTRL_RESET_BBP` | BIT(1) | mt76x02_regs.h:271 |
| `MT_MAC_SYS_CTRL_ENABLE_TX` | BIT(2) | mt76x02_regs.h:272 |
| `MT_MAC_SYS_CTRL_ENABLE_RX` | BIT(3) | mt76x02_regs.h:273 |
| `MT_MCU_IVB_SIZE` | 0x40 | mt76x0/mcu.h:14 |
| `MT_MCU_DLM_OFFSET` | 0x80000 | mt76x0/mcu.h:15 |
| `MCU_FW_URB_MAX_PAYLOAD` | 0x38f8 = 14584 | mt76x0/usb_mcu.c:13 |
| `MCU_RESP_URB_SIZE` | 1024 | mt76.h:661 |
| `MCU_RESP_TIMEOUT_MS` | 300 | mt76x02_usb_mcu.c:46 |
| `MCU_RESP_MAX_RETRY` | 5 | mt76x02_usb_mcu.c:44 |
| `MT_MCU_MSG_LEN` | GENMASK(15,0) | mt76x02_dma.h:33 |
| `MT_MCU_MSG_CMD_SEQ` | GENMASK(19,16) | mt76x02_dma.h:34 |
| `MT_MCU_MSG_CMD_TYPE` | GENMASK(26,20) | mt76x02_dma.h:35 |
| `MT_MCU_MSG_PORT` | GENMASK(29,27) | mt76x02_dma.h:36 |
| `MT_MCU_MSG_TYPE_CMD` | BIT(30) | mt76x02_dma.h:38 |
| `MT_RX_FCE_INFO_CMD_SEQ` | GENMASK(19,16) | mt76x02_dma.h:25 |
| `MT_RX_FCE_INFO_EVT_TYPE` | GENMASK(23,20) | mt76x02_dma.h:26 |
| `EVT_CMD_DONE` | 0 | dma.h:150-158 (enum, first member) |
| `CPU_TX_PORT` | 2 | mt76x02_dma.h:43-51 (enum) |
| `Q_SELECT` | 1 | mt76x02_mcu.h:63 (enum mcu_function) |
| `MT_MCU_MEMMAP_WLAN` | 0x410000 | mt76x02_mcu.h:19 |
| `MT_VEND_DEV_MODE` | 0x01 | (shared mt76 vendor reqs) |
| `MT_VEND_MULTI_WRITE` | 0x06 | mt76x02_usb_core.c |
| `MT_VEND_MULTI_READ` | 0x07 | mt76x02_usb_core.c |
| `MT_VEND_WRITE_FCE` | 0x42 | mt76x02_usb_core.c |
| `MT_VEND_REQ_MAX_RETRY` | 10 | usb.c:11 |
| `MT_VEND_REQ_TOUT_MS` | 300 | usb.c:12 |
| `MT_EE_MAC_ADDR` | 0x004 | mt76x02_eeprom.h:15 |
| `MT_EE_NIC_CONF_0` | 0x034 | mt76x02_eeprom.h:19 |
| `MT_EE_NIC_CONF_1` | 0x036 | mt76x02_eeprom.h:20 |
| `MT_EE_FREQ_OFFSET` | 0x03A | mt76x02_eeprom.h:23 |
| `MT_EE_USAGE_MAP_START` | 0x1E0 | mt76x02_eeprom.h:92 |
| `MT_EE_USAGE_MAP_END` | 0x1FC | mt76x02_eeprom.h:93 |
| `BOARD_TYPE_2GHZ` | 1 | mt76x02_eeprom.c:80 |
| `BOARD_TYPE_5GHZ` | 2 | mt76x02_eeprom.c:77 |
