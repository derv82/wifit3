# AR9271 (ath9k_htc) Custom Python USB Driver: Device Hangs/Fails to Re-enumerate After Firmware Upload via EP0

**Background:**
I am writing a Userland (PyUSB/libusb) Python driver to control the Atheros AR9271 (Alfa AWUS036NHA) natively on Windows using the WinUSB driver. The goal is to bypass the OS wireless stack entirely and manipulate the Soft-MAC via HTC/WMI commands.

I have successfully reverse-engineered the WMI channel hopping sequences and register blasts (synthesizer `0x9874`), but I am stuck on the "Cold" initialization phase—specifically, bridging the gap between uploading the firmware and getting the device to execute it and re-enumerate with its Bulk/Interrupt endpoints.

**The Setup & Environment:**
*   **OS:** Windows (Using Zadig to replace the default driver with `WinUSB`).
*   **Hardware:** Alfa AWUS036NHA (VID: `0x0CF3`, PID: `0x9271`).
*   **Firmware:** Byte-for-byte extraction of `htc_9271.fw` (version 1.4.0) captured from a working Linux `airmon-ng` session via Wireshark/usbmon. Size: `51008 bytes`.
*   **Initial State:** When plugged in, the "Cold" device presents `0x0CF3:0x9271` with 6 endpoints, but EP4 (`0x04`) is an **Interrupt OUT** endpoint (confirming firmware is not yet running).

**The Upload Process (Which appears successful):**
Based on PCAP analysis of a working Linux driver initialization, I am blasting the 51008-byte firmware image to the device in 4096-byte chunks using USB Control Transfers (EP0). 

*   `bmRequestType` = `0x40` (Vendor, Host-to-Device)
*   `bRequest` = `0x30`
*   `wValue` = `(Address >> 8) & 0xFFFF`
*   `wIndex` = `(Address >> 24) & 0xFF`

The upload starts at memory address **`0x501000`** (derived from the PCAP showing the first chunk using `wValue=0x5010`, `wIndex=0x00`).

The Python PyUSB loop successfully uploads all 51008 bytes without any timeouts or stalls.

**The Boot Trigger & The Problem:**
After the upload finishes, I attempt to trigger the firmware execution. Based on standard `ath9k_htc` behavior, I send a 0-byte Control Transfer to the base execution address:

*   `bmRequestType` = `0x40`
*   `bRequest` = `0x30`
*   `wValue` = `0x5010`
*   `wIndex` = `0x00`
*   `Payload` = `b''` (0 bytes)

**Expected Behavior:** The AR9271 should accept the command, soft-reset its USB core, physically drop off the bus (triggering a Windows "device disconnected" sound), and re-enumerate. Upon re-enumeration, EP4 should transition from `Interrupt OUT` to `Bulk OUT` (ready for WMI HTC commands).

**Actual Behavior:**
1.  The 0-byte boot command is sent.
2.  The device *does* drop the USB connection at the PyUSB level (we catch the expected `usb.core.USBError` pipe error).
3.  **However, Windows does not play a disconnect/reconnect sound, and Zadig does not detect a state change.**
4.  If I immediately loop and run `usb.core.find(idVendor=0x0cf3, idProduct=0x9271)`, PyUSB *still finds the device handle* on the bus.
5.  When I attempt to call `dev.set_configuration()` or `dev.get_active_configuration()` on this "post-boot" handle, Windows throws a hard **`[Errno 13] Access denied (insufficient permissions)`**. It will endlessly throw Errno 13 no matter how long I wait.

**The "Smoking Gun":**
On Windows/WinUSB, Errno 13 after a firmware blast implies the device did soft-reset, but failed to load the firmware and fell back into a "DFU" or "Error" state. Because the new descriptors are "broken" (due to the firmware crash), Windows marks the device as "Malfunctioning" and blocks all further access.

**My Hypotheses / Questions for the Community:**

1.  **The "Final Chunk" vs. The "Boot" Command:** If I am using `bRequest = 0x30` for everything, I am just writing to memory. Does the AR9271 BootROM require a different `bRequest` or a specific `wIndex` flag on the very last chunk to signal "Download Complete; Jump to Entry Point"?
2.  **The Address Offset (0x501000 vs 0x501100):** I am uploading to `0x501000`. Does the `htc_9271.fw` file contain a small header (e.g., 16 or 32 bytes) that I need to strip before uploading, or do I need to offset the load address by the header size to prevent the CPU from executing the header as invalid instructions?
3.  **CPU Reset / Watchdog:** Should I be using `REG_WRITE` (`bRequest` 0x05) to clear the 'Watchdog' or 'Reset' registers *before* jumping to `0x501000`? Does the entry point need to be explicitly set in the `CPU_CFG` register, or does the 0-length transfer to `0x501000` suffice?
4.  **Memory Settling:** Is there a required delay between the last firmware block and the jump command to allow the memory controller to settle?

Any guidance on the exact sequence of USB Control Transfers required to reliably transition the AR9271 from the "Cold" BootROM state to the "Warm" Firmware execution state would be greatly appreciated.