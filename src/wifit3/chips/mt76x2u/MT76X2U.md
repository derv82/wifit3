# MT76x2U / MT7612U — Ground Truth

## Potential Known Gaps

Cross-driver gap classes (project audit 2026-05-25). **Offline analysis only —
no hardware available; verify before `[x]`.**

- [~] **RX polling loop drops frames** — PORTED to the shared RxReaderThread
  (RxDrainer now drives it; awaiting HW verify). Was the gap: `rx.py:_loop` (187)
  does `await transport.async_read_bulk(...)` (executor read) then decode +
  parse + callback on the event loop — no read posted while parsing. Same
  pattern as rtl8821au pre-fix. Fix: dedicated reader thread + queue hand-off
  (rtl8821au commit 2e3a7a7).
- [x] **RX filter / monitor mode (ToDS capture)** — ALREADY HANDLED (offline
  verdict; the "working monitor sibling" per the monitor-deviation memory).
  `mac.py:mac_start(monitor=True)` (309-328) clears `MT_RX_FILTR_CFG`
  DROP_UC_NOME(bit2) + DROP_NOT_MYBSSID(bit3); `mac.py:301-304` writes bare MAC
  to `MT_MAC_ADDR_DW1`/`MT_MAC_BSSID_DW1` (no U2ME/MBSS drop). ToDS should work.

Verified facts only. Hypothesis-level material goes in commit messages,
not here. `[SRC]` = kernel source (`data_dumps/mt76-source-v6.18/`). `[WIRE]`
= pcap evidence (`usb_dumps/captures_mt76x2u/capture-1.pcap`).

## Identity

- **Family**: mt76, generation `mt76x02` (older sibling of mt76_connac).
- **Kernel module**: `mt76x2u` (vs `mt7921u` for the WiFi-6 sibling).
- **VID:PID claimed**: 15 entries — see `constants.py::USB_IDS_MT76X2U`. The
  card on the dev machine right now is **`0e8d:7612`** = Alfa AWUS036ACM.
  `[SRC]` `mt76x2/usb.c:12`.
- **USB 3.0 capable**. `bcdUSB=0x0300`, PyUSB speed=4 (SUPER).
  `[WIRE]` live pyusb dump on `0e8d:7612`.

## Endpoint layout

After the boot-ROM/mass-storage→wireless mode switch (which Windows + WinUSB
triggers automatically on first open), interface 0.0 exposes 2 bulk-IN + 6
bulk-OUT, ALL at 1024-byte maxPacketSize. Kernel enum order is positional —
endpoints are assigned to the `mt76u_in_ep` / `mt76u_out_ep` slots in
descriptor order. `[SRC]` `usb.c:292` (`mt76u_set_endpoints`).

| Slot | Kernel name | Address |
|---|---|---|
| in_ep[0]  | `MT_EP_IN_PKT_RX`      | **0x84** |
| in_ep[1]  | `MT_EP_IN_CMD_RESP`    | **0x85** |
| out_ep[0] | `MT_EP_OUT_INBAND_CMD` | **0x08** |  ← FW upload + MCU
| out_ep[1] | `MT_EP_OUT_AC_BE`      | 0x04 |
| out_ep[2] | `MT_EP_OUT_AC_BK`      | 0x05 |
| out_ep[3] | `MT_EP_OUT_AC_VI`      | 0x06 |
| out_ep[4] | `MT_EP_OUT_AC_VO`      | 0x07 |
| out_ep[5] | `MT_EP_OUT_HCCA`       | 0x09 |

`[WIRE]` pyusb descriptor dump; matches kernel descriptor-iteration order.

## Cold-boot mass-storage stub (avoid)

Before the wireless EP set is exposed, the device enumerates as USB Mass
Storage SCSI BBB with EPs **0x81 IN / 0x02 OUT**. `[WIRE]` capture-1 frames
1-6 (`bInterfaceClass=0x08`). On Windows the WinUSB Zadig binding plus the
first `usb_reset_device`-equivalent open causes the device to re-enumerate
into wireless mode automatically — confirmed by the live pyusb dump showing
the wireless EP set with no manual switch. `transport.assert_expected_endpoints()`
fails fast with an actionable error if the wireless EPs are still missing.

## Register-access protocol

Every register read/write is one vendor control transfer. The 32-bit
address has two virtual-bus marker bits at the top; the kernel strips them
and picks bRequest accordingly. `[SRC]` `usb.c:85` (`__mt76u_rr`) and
`usb.c:111` (`__mt76u_wr`).

| Bus | Address marker | Read bReq | Write bReq |
|---|---|---|---|
| Default (MAC/BB/RF) | none | `0x07 MT_VEND_MULTI_READ` | `0x06 MT_VEND_MULTI_WRITE` |
| CFG bus             | `BIT(30)` | `0x47 MT_VEND_READ_CFG`  | `0x46 MT_VEND_WRITE_CFG` |
| EEPROM              | `BIT(31)` | `0x09 MT_VEND_READ_EEPROM` | — |

Encoding: `wValue = addr >> 16`, `wIndex = addr & 0xFFFF`, payload = 4-byte
little-endian value.

## Firmware upload — two stages

**Stage 1 — ROM patch** (`mt7662_rom_patch.bin`):

- `[SRC]` `mt76x2/usb_mcu.c:57` (`mt76x2u_mcu_load_rom_patch`).
- **MT7612 special-case**: `rom_protect = !is_mt7612(dev)` evaluates **false**
  for our silicon — the `MT_MCU_SEMAPHORE_03` acquisition is skipped entirely.
  This is the structural reason MT7612U doesn't hit the patch-semaphore wall
  that paused MT7921AU. `[SRC]` `usb_mcu.c:59`.
- Vendor reset (`MT_VEND_DEV_MODE`, `wValue=0x0001`).
- FCE config writes (PSE_CTRL=1, base_ptr=0x400230, max_cnt=1, pdma=0x44,
  skip_fs=3) and USB-DMA CFG (BULK_EN bits + RX_BULK_AGG_TOUT=0x20).
- Each chunk (max 2048 B) uploaded via the bulk-OUT path on **EP 0x08**:
  1. `MT_VEND_WRITE_FCE` to `MT_FCE_DMA_ADDR` (0x0230) — `mt76u_single_wr`
     splits the 32-bit dst across TWO control transfers (low half in wValue
     at addr+0, high half in wValue at addr+2, **no payload either**).
  2. Same dance for `MT_FCE_DMA_LEN` (0x0234) with `len << 16`.
  3. Bulk-OUT on EP 0x08: `[4B mt76 info: PORT|LEN|TYPE_CMD][chunk bytes][4B zero pad]`.
  4. RR + ++ + WR on `MT_TX_CPU_FROM_FCE_CPU_DESC_IDX` (0x09a8).
- After all chunks: `enable_patch` + `reset_wmt` (two raw vendor packets
  with hard-coded payloads — see kernel for byte sequences).
- Poll: rev≥E3 → `MT_MCU_CLOCK_CTL` BIT(0) goes high. Pre-E3 → `MT_MCU_COM_REG0`
  BIT(1).

**Stage 2 — Main firmware** (`mt7662.bin`):

- `[SRC]` `mt76x2/usb_mcu.c:144` (`mt76x2u_mcu_load_firmware`).
- Vendor reset again.
- FCE + USB-DMA CFG re-programmed (same values).
- **ILM** (instruction memory) uploaded to `0x00080000`, chunks up to 14592 B.
- **DLM** (data memory) uploaded to `0x00110000`, **or `0x00110800` if rev ≥ E3**.
- IVB trigger: `MT_VEND_DEV_MODE` `wValue=0x0012`.
- Poll: `MT_MCU_COM_REG0` BIT(0) goes high → FW is running.

## Pcap-derived FW sizes (capture-1)

`extract_mt7662_fw.py` walks `usb_dumps/captures_mt76x2u/capture-1.pcap` and
splits the bulk-OUT chunks by section. Outputs land in `assets/`:

| File | Bytes | Target |
|---|---|---|
| `mt7662_rom_patch_body.bin` | 26320 | `0x00090000` |
| `mt7662_ilm.bin`            | 64448 | `0x00080000` |
| `mt7662_dlm.bin`            | 17428 | **`0x00110800`** (confirms rev ≥ E3) |

DLM target = `0x110000 + 0x800` → the dev machine's MT7612U is **rev E3+**.

Headers from linux-firmware (`mt76x02_patch_header` for ROM patch, 32-byte
`mt76x02_fw_header` for main FW) never appear on the wire. We ship the
header-stripped bodies and `firmware.py` skips the header-read step.
`[[firmware-extraction]]` precedent.

## Verified wire facts (capture-1)

- **ASIC version = `0x76120044`** → rev **E4** (low byte 0x44). `[WIRE]`
  control READ bReq=0x07 of reg 0x0000, frame 137/138. Confirms the
  rev-≥E3 inference from the DLM offset, and pins it precisely at E4.
- **Inject TXWI** (aireplay-ng directed deauth, frame 32207): `rate=0x0000`
  (CCK 1 Mbps), `wcid=0xff`, `txstream=0x13` (2x2 MIMO, rev≥E4 branch),
  `pktid=0x00` (MT_PACKET_ID_NO_ACK — wcid-less frames never get a status
  push). `[WIRE]` `[SRC]` mt76x02_mac.c:397, tx.c:132.
- **MCU LOAD_CR** (frame 2457): `cmd=2`, `len=8`, payload
  `02 00 00 00 | ff 00 00 80` → cr_mode=`MT_RF_BBP_CR`(2), cfg=`0x800000FF`
  (BIT(31) | NIC_CONF nibbles). One LOAD_CR per init.
- **MCU SWITCH_CHANNEL_OP is sent twice** per channel switch (~13 ms apart):
  first with `ext_chan=0x00`, then `ext_chan=0xe0 + bw_index`. e.g. ch1
  frames 3005/3009.

## Open / unknown

- **TSSI is gated OFF by default** (`driver.py`: `_tssi_enabled` requires
  both the EEPROM flag and `WIFIT3_MT76X2U_TSSI=1`). This deviates from the
  kernel, which trusts the EEPROM. The periodic `tssi_compensate` path is
  suspected of zeroing TX power on this silicon (observed `tssi_slope=127`,
  near max). The `phy.py` port of `mt76x2_phy_tssi_compensate` audited as
  faithful, so the root cause is more likely in the EEPROM read feeding it
  or the monitor-mode `avg_rssi_all=-75` placeholder. Needs hardware
  diagnosis before flipping the default back to kernel behavior.
- Whether the wireless-mode endpoints we see are stable across power
  cycles, or whether some interactions stall mid-mode-switch (the
  `assert_expected_endpoints` guard is the early-detection mechanism).
- Channel-switch quirk noted in NEXT-STEPS: "channel switches need ~2 s
  of breathing room". Not yet replicated against the wifit3 driver — M5
  will baseline + bake in the delay if it's a real firmware constraint.
