# Handoff: Wifit3 AR9271 Driver - Intelligence & Integration

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented a fully functional Userspace Python driver for the Atheros AR9271 (v1.4). The driver can cold-boot, calibrate, tune to channels, and sniff live 802.11 management frames.

### Verified Milestones
1.  **Transport Asymmetry Solved:** `AR9271USBTransport` correctly handles the 4-byte HIF header on Bulk IN (0x82) while staying raw on Interrupt IN (0x83). It correctly pads to 32-bit DMA boundaries.
2.  **Handshake Stability:** 100% reliable 1,094-packet calibration marathon using strict sequential Stop-and-Wait logic. Timeout optimized to 0.2s for near-instant boot.
3.  **Dynamic Intelligence:** `WlanFrameParser` uses dynamic signature-scanning (32, 36, 40, 44, 48 window) to align radio frames, making it immune to alignment jitter. Added Broadcast MAC verification for management frames.
4.  **Signal Fidelity:** RSSI extraction correctly takes the `max()` of multiple antenna indices (8, 9, 11), providing accurate signal reporting (e.g., -73 dBm).
5.  **Clean Output:** Implemented an SSID Debouncer to prevent console spam. Raw USB noise is redirected to `usb_transactions.log`.

## NEXT STEPS (The Engine Layer)

### 1. The "Hot" Device Issue (Boot UX)
*   **Problem:** If the AR9271 is already initialized (firmware loaded), `test_hw.py` crashes because it tries to upload firmware to a "hot" device.
*   **Goal:** Update `wlan/manager.py` to detect if the device is already in a "hot" state (e.g., checking if the firmware upload endpoint is gone or if `0x1001` Target Ready is immediately available) and skip the boot sequence, jumping straight to sniffing. Avoid requiring unplug/replug.

### 2. The Generic Driver Abstraction (Architecture)
*   **Problem:** `AR9271Driver` directly parses and logs SSIDs. This tight coupling prevents supporting other chipsets (like RTL8187) and makes UI integration difficult.
*   **Goal:** Create a `GenericDriver` base class (or interface) that `AR9271Driver` extends.
*   **Mechanism:** The `WlanFrameParser` should return a raw dictionary of parsed data (not a Pydantic object to save CPU). The `GenericDriver` maintains a thread-safe `Dict[BSSID, AccessPoint]` registry and updates the Pydantic models (merging names, updating signal averages, incrementing beacon counts).

### 3. Full 802.11 Parsing (The `AccessPoint` Model)
*   **Task:** Expand `WlanFrameParser.parse_wmi_rx` to extract all fields required by `src/wifit3/engine/models.py`.
    *   **BSSID:** Bytes 16-21 of the 802.11 header.
    *   **Channel:** Parse the DS Parameter Set (Tag 3).
    *   **Encryption (WEP/WPA/WPA2/WPA3):** Parse the RSN (Tag 48) and Vendor Specific (Tag 221) Information Elements.
    *   **Beacons:** Driver should simply pass the parsed dict; `GenericDriver` increments the count.

### 4. Mobility (Channel Hopping)
*   **Task:** Implement a `ChannelScanner` loop in the `WlanInterface` (or Engine layer) that commands the driver to iterate through Channels 1-13.
*   **Mechanism:** Sleep for a dwell time (e.g., 200ms), call `driver.set_channel(ch)`, and repeat.

### 5. Integration (The UI Layer)
*   **Task:** Connect the `GenericDriver`'s list of `AccessPoint` objects to the Textual UI.
*   **Mechanism:** The UI should poll the `GenericDriver` or subscribe to an event stream to update the `airodump-ng` style dashboard in real-time.

## Architectural Guidelines
*   **Performance:** Do not instantiate a Pydantic `AccessPoint` model for every packet. Use native Python dicts in the hot path. Only update the Pydantic model when state changes or on a throttle timer.
*   **Lead's Rule:** Discuss class design (e.g., `GenericDriver` vs `WlanInterface` responsibilities) BEFORE execution. Treat the user as the Senior Lead.
