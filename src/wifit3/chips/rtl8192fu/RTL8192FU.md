# RTL8192FU

## Captured Wireless Card

- D-Link AN3U rev. A1, `2001:3328`.
- 2.4 GHz Wi-Fi 4, 2T2R, USB 2.0 high speed.
- Cold-boot capture: `src/wifit3/scripts/captures_rtl8192fu/capture-1.pcap`.
- Capture includes probe, monitor entry, RX, native hopping, and deterministic channel 1–14 tuning; it does not include injection.

## Linux Driver Source

- Upstream: `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`.
- Type: **mainline Linux `rtl8xxxu`**, not vendor/DKMS.
- Linux v7.2.6, commit `500df175a7f9e6bc1a9c328590ca5150f84f9ff0`, dated 2026-09-14.
- Vendored checkout: `driver_sources/rtl8xxxu-source-v7.2.6/drivers/net/wireless/realtek/rtl8xxxu/`.
- Chip implementation: `8192f.c`; shared USB/mac80211 implementation: `core.c`.

## Python Port Details

- `2001:3328` selects `RTL8192FUDriver` through `chips/rtl8192fu/__init__.py`.
- Status: complete cold initialization, monitor filtering, runtime RX activation, and 20 MHz channel tuning across channels 1–14 are replay-verified; `connect()` starts bulk-IN RX and applies the initial channel tune.
- Firmware: `assets/rtl8192fufw.bin`, 32416 bytes, SHA-256 `cce2c368238b01ebf68e257d9788bfba1481f11b4d126abf2553da2bb474fa5f`.
- Firmware was decompressed verbatim from Ubuntu's `linux-firmware` copy at `/lib/firmware/rtlwifi/rtl8192fufw.bin.zst` and byte-compared after decompression.
- Firmware redistribution uses Realtek's `LICENCE.rtlwifi_firmware.txt`, shipped beside the blob.

### Non-obvious in the port

- Endpoint order is significant: the captured card exposes bulk-IN `0x84`, interrupt-IN `0x87`, then bulk-OUT `0x05`, `0x06`, `0x08`, `0x09`, `0x0c`, and `0x0d` in descriptor order.
- Register access uses vendor request `0x05`, request types `0xc0`/`0x40`, index zero, and little-endian values.
- Mainline sets `needs_full_init = 1`; this port must reproduce the complete cold path.
- The 2-second rate/CFO watchdog is wire-silent in unassociated monitor mode: CFO samples require an associated matching BSSID, while the reset path retains the EFUSE crystal cap and initialized ATC state.
- Beacon work is AP/TX-only; RTL8192FU C2H work only updates Bluetooth coexistence or TX rate reports; bulk-IN resubmission is represented by `RxReaderThread`.
- Teardown follows mainline stop then `rtl8192fu_power_off`: stop RX/TX, disable RF, flush FIFO, enter LPS, reset firmware/MCU, then transition active → EMU → disabled.
- Jaguar-2 PHY-status pages 0/1/2 and both new/legacy CCK AGC RSSI paths are decoded; per-chain values use mainline's stronger-path rule.

## Known Problems

- TX is not yet integrated because the capture has no injection segment.
- The available capture has no explicit injection segment, so TX cannot be byte-verified or implemented from evidence.
- The capture has no teardown segment, so the source-ported power-off sequence is unit-tested but cannot be pcap byte-verified.

## Driver Entry Points

- USB register transport: `transport.py` → `RTL8192FUTransport`.
- Chip identification: `chip.py` → `identify_chip`.
- Driver lifecycle: `driver.py` → `RTL8192FUDriver`.
- Mainline probe: `core.c` → `rtl8xxxu_probe`.
- Mainline chip operations: `8192f.c` → `rtl8192fu_fops`.

## Scripts

- `scripts/chips/rtl8192fu/extract_tables.py` regenerates the chipset-local Python tables from pinned mainline `8192f.c`.
- `scripts/chips/rtl8192fu/verify_pcap.py` replays 5130 cold-init/runtime/monitor operations plus every deterministic channel window and captured bulk-IN RX; RX verifies 372 frames, 303 parsed packets, and `-99..-47 dBm` RSSI.
- `scripts/chips/rtl8192fu/test_hw.py` performs an RX-only cold-boot smoke test on an optional channel without transmitting.
- `src/wifit3/scripts/capture.py` records the generic cold-boot, RX, channel-tune, and optional TX reference bundle; run it with `--bssid2g`, `--channel2g`, and `--client2g` for an authorized RTL8192FU TX capture.

```bash
sudo uv run python -m wifit3.scripts.capture \
  --bssid2g aa:bb:cc:dd:ee:ff \
  --channel2g 1 \
  --client2g 11:22:33:44:55:66
```

## Debug log

- 2026-10-20: TX byte matching remains blocked on a capture containing an authorized injection target.
