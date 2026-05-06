### `NEXT-STEPS.md`

#### Phase 1: Linux Validation & Optimization (Immediate)
Before expanding the scope, we must prove the core premise: cross-platform execution using the exact same user-space driver.
* **Linux Hot-Wire:** Boot into a Linux Live USB, detach the kernel driver (`sudo rmmod rtl8187`), and run the existing Windows MVP script.
* **Speed Run:** The native Linux driver is conservative with initialization timing. Experiment with compressing the micro-delays in `boot_sequence.py` (e.g., clamp max sleep to 5ms or 10ms) to outpace `airmon-ng`'s boot time without crashing the baseband Phase-Locked Loops.

#### Phase 2: TUI Integration & API Refactoring (Short-term)
To connect the raw hardware bypass to a user-facing terminal interface, the monolithic MVP script needs to be broken down into a callable API.
* **State Machine Breakdown:** Split the 8,000-command `FULL_BOOT_SEQUENCE` into logical chunks so the TUI can trigger specific actions.
  * `adapter.boot_into_monitor_mode()`
  * `adapter.listen()` (Async background thread)
  * `adapter.send_deauth(bssid, client)`
* **Event Emitter / Callback Architecture:** Design an async event loop where the background USB reading thread parses `dpkt` / native 802.11 frames and emits events (`on_beacon`, `on_handshake`, `on_ack`) back to the TUI to update the UI without blocking the main thread.
* **The "Demo" Milestone:** Successfully execute a targeted deauth attack natively from Windows and capture the WPA handshake in the TUI, proving parity with native Linux tools.

#### Phase 3: Hardware Expansion (Mid-term)
The RTL8187 is mapped. The process must now be repeated to prove the capture-and-crack pipeline works on modern chipsets.
* **Acquisition:** Source major chipset families currently supported by commercial injection drivers (e.g., Atheros, Ralink, modern Realtek/Mediatek).
* **Capture Pipeline:** Use USBPcap + CommView/Acrylic to generate `.pcap` files for these new cards.
* **Sequence Extraction:** Run the proven extraction scripts to pull the interleaved READ/WRITE/SET_CONFIG sequences and raw firmware blobs.

#### Phase 4: The Hardware Abstraction Layer (Long-term)
Do not build the abstraction layer until 3 or 4 distinct chipsets are fully mapped. Once the common lifecycle patterns are understood, build the bridge.
* **Generic Interface:** Define the standard operations every card must fulfill regardless of underlying hardware.
* **Implementation Variants:**
  * `RegisterBlaster`: For older chipsets (like the RTL8187) that require thousands of individual control transfers to initialize registers.
  * `FirmwareBlaster`: For modern chipsets that require a contiguous binary blob uploaded via Bulk endpoints before control transfers are accepted. 
