# Handoff: Wifit3 AR9271 Driver - From Core to Intelligence

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented a fully functional Userspace Python driver for the Atheros AR9271 (v1.4). The driver can cold-boot, calibrate, tune to channels, and sniff live 802.11 management frames.

### Verified Milestones
1.  **Transport Asymmetry Solved:** `AR9271USBTransport` correctly handles the 4-byte HIF header on Bulk IN (0x82) while staying raw on Interrupt IN (0x83).
2.  **Handshake Stability:** 100% reliable 1,094-packet calibration marathon using strict sequential Stop-and-Wait logic.
3.  **Dynamic Intelligence:** `WlanFrameParser` uses dynamic signature-scanning (32/36 window) to align radio frames, making it immune to alignment jitter.
4.  **Signal Fidelity:** RSSI extraction correctly take the `max()` of multiple antenna indices, providing accurate signal reporting (approx -73 dBm).
5.  **Jailed Debugging:** All raw USB noise is redirected to `usb_transactions.log`, keeping the framework console clean and high-signal.

## Immediate Next Steps (Intelligence & Mobility)

### 1. Spatial Intelligence (Channel Extraction)
Currently, we can tune to a channel, but we don't verify it in the packet.
*   **Task:** Update `WlanFrameParser` to extract the **DS Parameter Set (Tag 3)** from Beacons. This allows us to log the network's current channel and verify if our tuner is on-target.
*   **Goal:** Log: `[SSID] Captured 'Beachball 2.4' on CH 6 (RSSI: -65 dBm)`.

### 2. Full Mobility (Channel Scanning)
We have the tuning tables, but no orchestrator.
*   **Task:** Implement a `ChannelScanner` loop in `WlanInterface` that iterates through Channels 1-13 with a specific dwell time (e.g., 200ms).
*   **Goal:** A live network list showing every SSID in the environment across all channels.

### 3. Aggression (Packet Injection)
The next major architectural wall.
*   **Task:** Implement `WlanPacketSerializer` to create raw 802.11 Deauth/Disassoc frames and send them via the TX pipe (EP 1).
*   **Verification:** Replay a known-good PCAP deauth burst and verify on a target device.

## Architectural Guidelines
*   **Maintain Asymmetry:** Always remember: Bulk OUT (0x04) needs a 4-byte HIF + 4-byte WMI header. Bulk IN (0x82) needs 4-byte HIF stripping.
*   **Stay Native:** Avoid adding heavy libraries (Scapy). Maintain the native `WlanFrameParser` for maximum performance and minimum dependencies.
*   **Lead's Rule:** Discuss API design BEFORE execution. Treat the user as the Senior Lead.
