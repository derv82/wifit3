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
- Status: complete cold initialization, runtime RX activation, and 20 MHz channel tuning across channels 1–14 are replay-verified; `connect()` starts bulk-IN RX and `set_channel()` uses the source tune path.
- Firmware: `assets/rtl8192fufw.bin`, 32416 bytes, SHA-256 `cce2c368238b01ebf68e257d9788bfba1481f11b4d126abf2553da2bb474fa5f`.
- Firmware was decompressed verbatim from Ubuntu's `linux-firmware` copy at `/lib/firmware/rtlwifi/rtl8192fufw.bin.zst` and byte-compared after decompression.
- Firmware redistribution uses Realtek's `LICENCE.rtlwifi_firmware.txt`, shipped beside the blob.

### Non-obvious in the port

- Endpoint order is significant: the captured card exposes bulk-IN `0x84`, interrupt-IN `0x87`, then bulk-OUT `0x05`, `0x06`, `0x08`, `0x09`, `0x0c`, and `0x0d` in descriptor order.
- Register access uses vendor request `0x05`, request types `0xc0`/`0x40`, index zero, and little-endian values.
- Mainline sets `needs_full_init = 1`; this port must reproduce the complete cold path.

## Known Problems

- TX is not yet integrated because the capture has no injection segment.
- RX descriptor framing is ported, but Jaguar-2 PHY-status RSSI decoding is pending; received packets currently carry a placeholder `-50 dBm` RSSI.
- The available capture has no explicit injection segment, so TX cannot yet be byte-verified.

## Driver Entry Points

- USB register transport: `transport.py` → `RTL8192FUTransport`.
- Chip identification: `chip.py` → `identify_chip`.
- Driver lifecycle: `driver.py` → `RTL8192FUDriver`.
- Mainline probe: `core.c` → `rtl8xxxu_probe`.
- Mainline chip operations: `8192f.c` → `rtl8192fu_fops`.

## Scripts

- `scripts/chips/rtl8192fu/extract_tables.py` regenerates the chipset-local Python tables from pinned mainline `8192f.c`.
- `scripts/chips/rtl8192fu/verify_pcap.py` replays cold init/runtime start plus every deterministic channel window; cold/start covers 4880 operations and each channel 2–14 window covers 494 operations with zero divergence.
- `scripts/chips/rtl8192fu/test_hw.py` performs an RX-only cold-boot smoke test on an optional channel without transmitting.

## Debug log

- 2026-10-20: TX byte matching remains blocked on a capture containing an authorized injection target.
