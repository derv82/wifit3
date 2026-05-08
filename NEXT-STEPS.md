# Handoff for Next Agent: Wifit3 AR9271 Driver

## Current State: Massive Breakthrough
We have successfully reverse-engineered and implemented the complex "Soft-MAC" initialization for the Atheros AR9271 (ath9k_htc) directly in Python using PyUSB. We completely abandoned the `mac80211` assumptions and are now speaking the hardware's native protocol.

The script `scripts/ar9271/main.py` successfully:
1. **Detects** if the device is "Cold" (uninitialized, lacking Bulk endpoints).
2. **Uploads Firmware:** Blasts `htc_9271_cleanroom.fw` in 512-byte chunks to EP0.
3. **Boots the MCU:** Sends the precise `0x31` Boot Command and `0x23` CPU Wakeup/Reset command to execute the firmware.
4. **Re-connects:** Re-finds the device without a physical unplug, as the hardware transitions EP4 to a Bulk/Interrupt OUT endpoint and exposes the data pipes (EP82/EP83).
5. **HTC Connect Sequence:** (The breakthrough!) The script polls EP 0x83 for the `HTC_MSG_READY_ID` (0x0001), then sends an `HTC_MSG_CONNECT_SERVICE_ID` (0x0002) for the WMI Service (0x0100) on EP 0x04. The firmware successfully ACKs this and assigns a logical endpoint for WMI.
6. **WMI State Machine:** The `WMIManager` now wraps the `Channel 6` Golden Sequence (a massive series of PHY/AGC register calibration writes) with properly incrementing, Big-Endian Sequence IDs.

## The Immediate Issue: Command 6 Timeout
Because the HTC Connect Sequence was implemented, the hardware successfully received and ACK'd the first **5** WMI commands of our Channel 6 Golden Sequence on EP 0x04.

However, it times out (Errno 10060) on **Command 6**.

```text
[*] Starting USB single-consumer thread...
[*] Executing Golden Sequence for Channel 6...
    -> Firing: [ 6 / 67 ] [-] USB Write Error (Seq 6): [Errno 10060] Operation timed out
```

## Next Steps for the Agent
Your mission is to fix the timeout starting at Command 6 in the Golden Sequence.

**Theories to Investigate:**
1. **HTC Credits Exhaustion:** Our `wmi_state.py` expects Credit Reports on the Bulk IN pipe, but it might not be parsing them correctly or from the right endpoint. If the script blasts 5 commands, it might have consumed the initial 5 credits granted by the firmware. When it tries to send the 6th, the firmware's RX buffer is full, so it drops the USB OUT transaction (causing the `10060` timeout). You need to verify if the single-consumer thread (`usb_manager._reader_loop`) is actually receiving and parsing Credit Reports from EP 0x82 or EP 0x83.
2. **Logical Endpoint Routing:** The HTC Connect Response told us which *Logical Endpoint* the WMI service was mapped to (e.g., `Assigned to HTC Endpoint: 01`). Our `ar9271_golden_template.py` hardcodes the HTC Header (the first 6 bytes of the WMI packet). Are we sending the WMI commands to Endpoint `0x00` in the HTC header when we should be sending them to the assigned `0x01`? Check `wmi_state.py` and the Golden Templates.
3. **Endpoint Polling Mismatch:** Are the HTC Credits or WMI ACKs arriving on EP 0x83 (Interrupt IN) instead of EP 0x82 (Bulk IN)? The `_reader_loop` only polls `self.ep_in` (which is EP 0x82). It might be missing the ACKs because it's not polling EP 0x83.

**Files to focus on:**
* `scripts/ar9271/main.py`
* `scripts/ar9271/usb_manager.py`
* `scripts/ar9271/wmi_state.py`
* `ar9271_golden_template.py` (and the `build_template.py` generator script)

Good luck. You are incredibly close to native packet injection.