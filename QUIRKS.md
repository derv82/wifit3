# Hardware Quirks & Documentation

## Ralink RTL8187L
*   **Architecture**: "Hard-MAC" (mostly). High-level USB control transfers handle most state changes.
*   **Initialization**: Requires a specific sequence of reads/writes to clear latches and calibrate baseband (see `boot_sequence.py`).
*   **Monitor Mode**: Hardware filter is bypassed implicitly if we sniff before the OS binds to it.

## Atheros AR9271 (ath9k_htc)
*   **Architecture**: "Soft-MAC". The hardware is essentially a dumb radio. The host driver (Linux kernel) performs complex math and orchestrates the radio via a firehose of memory-mapped register reads/writes.
*   **Encapsulation**: Commands are sent via USB Bulk transfers (EP4 Out). Payloads are wrapped in an HTC Header (8 bytes) + WMI Header (4 bytes).
*   **Sequence IDs**: Every WMI command *must* include a strictly incrementing 16-bit Sequence ID. Raw PCAP replay fails because the device tracks the expected sequence.
*   **Channel Hopping**: There is *no* high-level `WMI_SET_CHANNEL` command used in practice.
    *   The driver sends a massive sequence of `WMI_REG_WRITE` (0x15) commands (PHY warm-up tables, AGC calibration).
    *   The *actual* frequency lock is achieved by calculating a Fractional-N Synthesizer Word on the CPU and writing it to register **`0x9874`** (`AR_PHY_SYNTH_CONTROL`).
    *   **Channel 6 Word:** `0x30a27777`
    *   **Channel 1 Word:** `0x30a0cccc`
*   **Initialization**: `airmon-ng start` triggers a firm-ware level reset. The USB device disconnects and reconnects. The first command sent post-reset is `WMI_GET_REV` (0x000E) to establish the state.
*   **Endianness**: WMI Command IDs and Sequence IDs are **Big Endian**.

### Takeaway Strategy for AR9271
We must build a "Template Replay" engine. We capture the "Golden Sequence" of register writes for a channel hop, but we cannot replay it raw. We must dynamically inject our own incrementing sequence IDs into the WMI headers and swap out the 4-byte payload for register `0x9874` depending on our target channel.