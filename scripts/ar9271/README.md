# AR9271 (ath9k_htc) PyUSB Driver MVP

This directory contains the Python-native (Userland) implementation for controlling the Atheros AR9271 chipset via PyUSB. This bypasses traditional OS wireless stacks (like `mac80211` / `aircrack-ng`) by talking directly to the hardware using WinUSB (via Zadig on Windows) or libusb.

## Architectural Overview: The "Soft-MAC" Reality

Unlike older chipsets (e.g., RTL8187) which provide high-level USB commands for "set channel" or "sniff", the AR9271 operates as a "Soft-MAC" device. 

1. **Dumb Radio, Smart Driver:** The hardware is essentially a remote-controlled radio. The host CPU (this Python script) acts as the firmware's brain.
2. **Register Blasts:** There is no `WMI_SET_CHANNEL` command used in practice. Channel hopping requires sending hundreds of `WMI_REG_WRITE` (0x15) commands to manually configure AGC calibration and PHY warm-up tables.
3. **Fractional-N Synthesizer:** The actual frequency lock is achieved by calculating a Fractional-N Synthesizer word and injecting it directly into register `0x9874` (`AR_PHY_SYNTH_CONTROL`).
   * Channel 1: `0x30a0cccc`
   * Channel 6: `0x30a27777`
4. **Stateful Conversation:** Every WMI command sent to the device must include a strictly incrementing 16-bit Sequence ID. The device tracks these. Raw PCAP replay of initialization sequences will fail because the sequence IDs will mismatch the device's internal state.

## Core Components

The architecture is divided into modular, thread-safe components to handle the complex state machine required by the AR9271.

*   `main.py`: The entry point. Initializes PyUSB, locates the device, and orchestrates the `USBManager` and `AR9271Scanner`.
*   `usb_manager.py`: **Critical Component.** Handles all raw USB reading and writing. 
    *   **Single-Consumer Model:** To prevent race conditions between sniffing and ACKs, a single background thread (`_reader_loop`) reads from the Bulk IN endpoint. It parses the incoming packets and dispatches them to thread-safe queues (`rx_queue` for Wi-Fi frames, `event_queue` for WMI ACKs).
    *   **Credit Tracking:** The AR9271 uses HTC Credits for flow control. `usb_manager` parses credit reports and ensures we don't overflow the device's TX FIFO during massive register blasts.
*   `wmi_state.py`: Manages the WMI state machine. Tracks and safely wraps the Sequence ID (`1-254`). Handles the Big Endian formatting required for WMI command headers.
*   `models.py`: (`AR9271Descriptors`) Handles the structural unpacking of hardware descriptors.
    *   **Endianness Warning:** While WMI headers use Big Endian, the `ath_rx_status` and `ath_tx_status` hardware descriptors prepended to 802.11 frames use **Little Endian**.
    *   This class safely extracts `rs_datalen` to perfectly slice the 802.11 frame out of the USB payload, avoiding heuristic-based parsing errors.
*   `scanner.py`: The high-level orchestrator. Uses the `usb_manager` to send "Golden Sequences" (captured register blasts) for channel hopping, dynamically injecting the correct synthesizer word for the target channel. Processes the `rx_queue` to parse and display 802.11 frames.
*   `ar9271_golden_template.py`: Generated file (via `build_template.py`) containing the raw byte arrays of the channel hopping sequences, with sequence IDs stripped and the synthesizer word replaced with a dynamic injection point.

## Known Caveats & Future Work

1.  **Firmware Upload:** This MVP assumes the firmware (`htc_9271.fw`) has already been uploaded to the device (e.g., by a previous run or a Kali VM). A complete standalone driver must implement the firmware upload sequence upon initial plug-in (when PID is `0x9271`, before it re-enumerates).
2.  **Calibration Tables:** The Golden Sequence relies on a static capture of the PHY calibration tables (the hundreds of register writes). Because these handle noise-floor calibration, replaying a capture taken in a different RF environment *might* result in a "deaf" card if the calibration is too specific to the original capture environment. Dynamic calculation of these tables is extremely complex and currently beyond scope.
3.  **TX Endpoint:** Packet injection (Deauths) usually requires sending the `ath_tx_status` + 802.11 frame to a *different* endpoint (e.g., EP2 or EP3) than the WMI control endpoint (EP4). This MVP currently focuses on control (EP4) and RX sniffing (EP1/2/3 IN).
4.  **TX NO_ACK Flag:** When implementing injection, you *must* set `TX_DESC_FLAG_NO_ACK` (0x01) in the `ath_tx_status` descriptor for unassociated frames (like Deauths). Otherwise, the firmware will hang waiting for an ACK from a client that isn't connected.
