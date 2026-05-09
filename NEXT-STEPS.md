# Handoff for Next Agent: Wifit3 AR9271 Driver

## Current State: Massive Breakthroughs & Framework Integration

We have successfully reverse-engineered and implemented the complex "Soft-MAC" initialization for the Atheros AR9271 (ath9k_htc) directly in Python. We have moved from standalone experimental scripts into the `src/wifit3/` framework.

### Verified Successes
1.  **Cold Boot Resolved:** Achieved reliable re-enumeration on Windows/WinUSB by using 512-byte firmware chunks, the `0x31` boot trigger, and properly calling `usb.util.dispose_resources(dev)` to allow the OS to transition the device.
2.  **6-Byte Protocol Alignment:** Empirically verified that the AR9271 expects a **4-byte HTC header followed by 2 bytes of padding**, creating a 6-byte offset for all payloads. This satisfies the hardware's 32-bit DMA alignment requirements.
3.  **Framework Stability:** The `WlanDeviceManager` now correctly initializes the `libusb_package` backend and loads actual firmware bytes (fixing a path-string bug).

## The Immediate Issue: Handshake Hang
While the firmware boots and re-enumerates, the `AR9271Driver.connect()` sequence in `driver.py` is currently "deaf" to the device's responses.

### Reproduction
1.  **Reset Hardware:** `uv run python scratch/test_reset.py` (Clears hung states without physical unplug).
2.  **Run Test:** `uv run python scratch/test_hw.py`.
3.  **The Hang:** The script will wait indefinitely at `[1/4] Waiting for HTC Ready on EP 0x83`.

### Why it Fails
The current `connect()` implementation uses a **sequential polling loop** (`while True: dev.read(...)`) which is prone to race conditions and missed packets. The device is likely sending its `HTC_MSG_READY_ID` and `HTC_MSG_CONNECT_SERVICE_RESPONSE_ID`, but the driver isn't listening at the exact moment they arrive.

## Next Steps (Immediate Mission)
Your mission is to transition the driver to an **Async-First Listener Architecture**.

1.  **Spawn Listeners Early:** The very first step of `connect()` should be to spawn background tasks for **both** EP 0x82 (Data/WMI) and EP 0x83 (Control/Credits).
2.  **Event-Driven Handshake:** Use `asyncio.Event` objects. The background listeners should parse the incoming stream and `.set()` the events when they see the correct `HTC_MSG_ID`.
3.  **Service Confirmation:** Capture the logical endpoint assigned in the connection response (usually `0x01`) and ensure all subsequent WMI commands are routed to that logical EP.
4.  **WMI Init:** Ensure `WMI_ATH_INIT_CMDID` (0x0006) is the first WMI command sent after the handshake is complete.

## Ground Truth References
*   **Protocol Structure:** See `src/wifit3/chips/ar9271/AGENTS.md` for the subagent tooling.
*   **Raw Traffic:** The `usb_transactions.log` is configured to log every raw byte—use it as the absolute source of truth for alignment.
*   **C Source:** Refer to `data_dumps/ath9k-source-v6.8/htc_hst.h` for official struct definitions.

## Communication Protocol (CRITICAL)
The user is a **Senior Lead Software Engineer**. 
1.  **Back-and-Forth Required:** Discuss architecture, API design, and technical rationale extensively BEFORE making any code changes.
2.  **Technical Pessimism:** Assume failure. Act as a grounded co-engineer, not a hype-man.
3.  **Brevity:** Keep responses brief, high-signal, and fluff-free.
4.  **No Hand-holding:** Propose the strategy, wait for confirmation, then implement surgically.
