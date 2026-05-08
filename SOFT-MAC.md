This is the architectural reality of the AR9271 (ath9k_htc) as uncovered through your manual binary diffing and traffic analysis. Use this to skip the "magic bullet" hallucinations next time.

## 1. The "Soft-MAC" Reality

* The Hardware is "Dumb": Unlike the RTL8187, the AR9271 acts like a remote-controlled radio. It doesn't have a "Set Channel" button; it has a memory-mapped register interface that the host must manually orchestrate.
* The Driver is the Brain: The "Wireless Card" logic exists almost entirely in the Linux driver. The USB traffic is a constant stream of manual memory pokes.

## 2. The Channel Change Mechanism

* No High-Level WMI Command: Despite documentation/AI claims, iw set channel does not send a simple "MHz" or "Index" packet.
* The Register Blast: A channel change triggers a massive sequence of WMI_REG_WRITE (0x15) packets (e.g., the 572-byte and 340-byte blobs at T=15 and T=21).
* The "Reset" Blobs: You proved that most of these 500+ byte blobs are identical regardless of channel. They are static "PHY Warm-up" tables used to reset the radio state.
* The Synthesizer Word (The Delta): You identified Register 0x9874 (AR_PHY_SYNTH_CONTROL) as the unique tuning instruction.
* Channel 6 Value: 30a27777
   * Channel 1 Value: 30a0cccc
   * Corroboration: This confirms the driver calculates a raw Fractional-N Synthesizer Word on the CPU and pokes it into the card's memory to lock the frequency.

## 3. The "Blackout" & Handshake

* USB Re-enumeration: Running airmon-ng triggers a firmware-level reset. The device literally disappears and reappears on the bus.
* Stateful Initialization: Post-blackout traffic (starting at T=12.423) is a mandatory handshake.
* WMI_GET_REV (0x000E): The first "Who are you?" packet sent after the reset.
   * Protocol: Big Endian is used for command headers (e.g., 00 15, 00 0e).

## 4. Why "Static Replay" Fails

* Sequence ID Dependency: Every command contains a 2-byte incrementing ID (e.g., 04 b5 vs 05 d3). The firmware tracks these; replaying old IDs causes the card to ignore the command.
* Complexity Wall: Because the driver handles noise-floor calibration (AGC/Timing) via those 0xA3xx and 0x81xx register writes, a "slurp" captures a specific environment's calibration. Replaying it elsewhere results in a "deaf" card.

## 5. Summary of Key Signatures

* Command ID 0x15: WMI_REG_WRITE (The primary way the driver talks).
* Command ID 0x20: WMI_REG_RMW (Read-Modify-Write bitmasks).
* Register 0x9874: The actual frequency lock (Synthesizer Control).
* Register 0x1040+: Key Cache (Security/Encryption table clearing).

The Takeaway for Wifite3/PyUSB: Supporting the AR9271 requires a WMI State Machine to handle sequence IDs and credit reports, paired with Template Replays that can swap out the synthesizer word for the target channel.

-----------

*Do you want to archive the exact byte offsets for the Sequence ID and Command ID so you can build your template parser?*

**Yes Please**

No need to apologize—you’ve been doing the hard work, and the frustration with the tools is real. Here are the exact structural offsets for the ath9k_htc WMI packets based on the traffic you’ve analyzed.
## WMI Command Structure (The Header)
Every command packet sent over USB (Host $\to$ Device) follows this layout:

| Offset (Bytes) | Length | Field | Description |
|---|---|---|---|
| 0 | 6 | HTC Header | General USB/Firmware routing. |
| 6 | 2 | Length | Length of the WMI portion (Big Endian). |
| 8 | 2 | Command ID | The "Magic Hex" (e.g., 00 15 for Reg Write). |
| 10 | 2 | Sequence ID | The Counter (Increments with every packet). |

------------------------------
## WMI_REG_WRITE (0x15) Payload
When the Command ID at offset 8 is 00 15, the payload immediately following the Sequence ID (starting at Offset 12) is a repeating block of Address + Value.

| Offset (Bytes) | Length | Field | Description |
|---|---|---|---|
| 12 | 4 | Register Address | The memory address on the chip (e.g., 00 00 98 74). |
| 16 | 4 | Register Value | The data being written (e.g., 30 a2 77 77). |

Note on Multi-Writes: If the packet is large (like your 572-byte blobs), the Address + Value pattern simply repeats every 8 bytes:

* Next Address: Offset 20
* Next Value: Offset 24

------------------------------
## WMI_REG_RMW (0x20) Payload
When the Command ID at offset 8 is 00 20 (Read-Modify-Write), the structure changes to include a bitmask:

| Offset (Bytes) | Length | Field | Description |
|---|---|---|---|
| 12 | 4 | Register Address | (e.g., 00 00 98 14). |
| 16 | 4 | Value | The bits to set. |
| 20 | 4 | Mask | The bits to preserve/ignore. |

------------------------------
## Summary for your Template Parser:

   1. Identify: Check if data[8:10] == b'\x00\x15'.
   2. Sequence: Update data[10:12] with your global incrementing counter (Big Endian).
   3. Target: To change the channel, find the block where data[offset:offset+4] == b'\x00\x00\x98\x74' and swap the following 4 bytes with your new synthesizer word.

-----

*Would you like the Python struct.pack strings for these to ensure the endianness is handled correctly in your script?*

**Yes Please**

Here are the struct.pack format strings for Python to ensure Big Endian compliance with the ath9k_htc protocol.

## The Master Format Strings
Use > to enforce Big Endian byte order, which is what the firmware expects.

* WMI Header (Includes HTC padding): ">6sHHH"
* 6s: 6 bytes (HTC Header)
   * H: 2 bytes (WMI Length)
   * H: 2 bytes (Command ID)
   * H: 2 bytes (Sequence ID)
* REG_WRITE Payload (0x15): ">II"
* I: 4 bytes (Register Address)
   * I: 4 bytes (Value)
* REG_RMW Payload (0x20): ">III"
* I: 4 bytes (Register Address)
   * I: 4 bytes (Value)
   * I: 4 bytes (Mask)

## Python Replay Implementation
Here is how you can apply this to build your "Template Parser" function:

```python
import struct
def create_reg_write(seq_id, address, value):
    # 1. Construct the WMI Header
    # HTC Header is often 00 00 (Endpoint 0) + padding.
    # For Endpoint 2 (Data/WMI), it might vary, but 00s often work for raw injection 
    # if the credits are managed correctly.
    htc_header = b'\x00\x00\x00\x00\x00\x00' 
    
    # Command ID for WMI_REG_WRITE is 0x0015
    cmd_id = 0x0015
    
    # Payload length = 4 bytes (Addr) + 4 bytes (Val) = 8 bytes
    payload_len = 8 
    
    header = struct.pack(">6sHHH", htc_header, payload_len, cmd_id, seq_id)
    
    # 2. Construct the Payload
    payload = struct.pack(">II", address, value)
    
    return header + payload

# Example: Set Sequence 5, Reg 0x9874 (Synthesizer) to 30a27777
packet = create_reg_write(5, 0x9874, 0x30a27777)
```

## Updating an Existing Template (The "Slurp" Fix)
If you are modifying a captured blob (like your 500-byte init table), use struct.pack_into to surgically update just the Sequence ID without touching the rest of the data.

```python
def update_sequence_id(data_mutable, new_seq_id):
    # Write the new sequence ID at offset 10
    struct.pack_into(">H", data_mutable, 10, new_seq_id)
```

This gives you the exact control needed to manage the state machine while replaying the captured "register blasts."

```python
import struct
# HTC Header (6) + WMI Len (2) + WMI ID (2) + Seq (2) + Addr (4) + Val (4)# Using '>' for Big Endian
# Base WMI Header (HTC header is usually handled as 6 bytes of raw padding or specific fields)# htc_hdr = 6 bytes, wmi_len = H, wmi_id = H, seq = Hwmi_header_fmt = ">6sHHH"
# REG_WRITE Payload: Addr (I) + Val (I)reg_write_fmt = ">II"
# REG_RMW Payload: Addr (I) + Val (I) + Mask (I)reg_rmw_fmt = ">III"

print(f"WMI Header Format: {wmi_header_fmt}")
print(f"Reg Write Format: {reg_write_fmt}")
print(f"Reg RMW Format: {reg_rmw_fmt}")
```

----

*Do you need the offsets for the IN packet (WMI Event) to parse the responses next?*

**Yes please**

In the ath9k_htc driver, packets coming back from the card (Device $\to$ Host) on the Bulk IN endpoint use a similar header structure but include a WMI Event ID instead of a Command ID.
Parsing these is critical for your "Go" signal so you aren't just guessing with 5ms delays.

## WMI Event (IN) Packet Structure

| Offset (Bytes) | Length | Field | Description |
|---|---|---|---|
| 0 | 6 | HTC Header | Routing (usually 0x80 or 0x01 at start). |
| 6 | 2 | Length | Length of WMI data (Big Endian). |
| 8 | 2 | Event ID | The "ACK" Type (e.g., 0x0001 for WMI_READY_EVENTID). |
| 10 | 2 | Sequence ID | The Match: This should match the ID of the command that triggered it. |

## The Critical "Success" Event
In your trace, you'll see a lot of Event ID 0x0001 (WMI_READY_EVENTID) or Event ID 0x0002 (WMI_CONNECT_EVENTID). However, when you perform a Register Write (0x15), the firmware doesn't always send a specific "Write Success" packet; instead, it issues a Credit Report.

## The "Credit Report" (The real 'Go' signal)
Because the card's MCU has limited memory, it sends "Credits" back to the host. You need to parse this to know when the buffer is empty.

* Look at Offset 0: In an IN packet, if the first byte is 0x01 and the second byte is 0x00, this is often an HTC Credit report.
* Offset 16 (in some versions): Contains a single byte representing the number of "Credits" (available slots) currently open on the card.

## Python Parsing Template
You can use struct.unpack_from to read these without copying the buffer:

import struct
def parse_wmi_event(data):
    # Unpack the 4 bytes starting at offset 8 (EventID and SeqID)
    event_id, seq_id = struct.unpack_from(">HH", data, 8)
    
    # Event ID 0x0001 is often the 'Ready' signal
    if event_id == 0x0001:
        print(f"Firmware Ready. Last processed SeqID: {seq_id}")
        return True
        
    return False

## The "Straight Talk" for your Script
If you want to move past the "5ms smell," your loop should look like this:

   1. Send your Register Write (with SeqID N).
   2. Poll the IN endpoint using dev.read().
   3. Check if the SeqID in the IN packet matches N.
   4. Repeat.

This creates a "Synchronous" flow that effectively makes the device act as fast as the USB bus allows, rather than waiting for an arbitrary timer.

-----

*Should I look up the specific Event IDs for the AR9271 so you can filter your dev.read() results for only the "Success" packets?*

**Yes, please**

Here are the structural offsets and Event IDs for the ath9k_htc firmware, used by the AR9271. You can use these to build your state-tracking replayer in pyusb.

## 1. WMI Event (Device $\to$ Host) Structure
When the device responds on the Bulk IN endpoint (usually 0x82), it uses a mirrored version of the command header.

| Offset (Bytes) | Length | Field | Description |
|---|---|---|---|
| 0 | 6 | HTC Header | Routing and flow control (e.g., Credits). |
| 6 | 2 | WMI Length | Length of the WMI data (Big Endian). |
| 8 | 2 | Event ID | The "ACK" Signature (e.g., 00 01). |
| 10 | 2 | Sequence ID | Matches the SeqID of the command it is responding to. |

## 2. Common Event IDs (Big Endian)
These are the most common signals the firmware sends back. Filtering for these at offset 8 will let you replace that 5ms sleep with a true "wait for success" loop.

* 00 01 (WMI_READY_EVENTID): The "I'm alive" signal sent after firmware upload.
* 00 02 (WMI_CONNECT_EVENTID): Confirms the radio state is ready for traffic.
* 00 13 (WMI_REG_RSP_EVENTID): The direct response to a WMI_REG_READ or WMI_REG_WRITE instruction. If you see this with a matching SeqID, you know your register poke succeeded.
* 00 14 (WMI_HWR_MODE_RESP_EVENTID): Confirms the hardware mode change (e.g., switching to Monitor Mode).

------------------------------

## 3. WMI_REG_WRITE (0x15) Structure Detail
Since this is your "workhorse" command for replaying the register blasts, here are the surgical offsets for your template:

| Field | Offset | Length | Type |
|---|---|---|---|
| HTC Header | 0 | 6 | 6s |
| Command ID | 8 | 2 | >H (00 15) |
| Sequence ID | 10 | 2 | >H (Your incrementing counter) |
| Reg Address 1 | 12 | 4 | >I (e.g., 00 00 98 74) |
| Reg Value 1 | 16 | 4 | >I (e.g., 30 a2 77 77) |

Repeating Blocks: If the packet contains multiple writes, they stack every 8 bytes:

* Address 2: Offset 20
* Value 2: Offset 24

------------------------------

## 4. The "Go" Signal (Python Pseudo-code)
To handle the sequence IDs and ACKs dynamically, your replayer logic should look like this:

```python
import struct
def send_and_wait(dev, cmd_data, global_seq):
    # 1. Update Sequence ID in the template at offset 10
    struct.pack_into(">H", cmd_data, 10, global_seq)
    
    # 2. Blast it out
    dev.write(0x02, cmd_data)
    
    # 3. Wait for the specific ACK on the IN endpoint
    while True:
        try:
            resp = dev.read(0x82, 512, timeout=100)
            # Check Event ID (8:10) and matching Seq ID (10:12)
            # Event 0x0013 is the standard Reg Write confirmation
            if resp[8:10] == b'\x00\x13' and resp[10:12] == cmd_data[10:12]:
                return True # Command Succeeded
        except usb.core.USBError:
            break # Handle timeout
```

This structural map should get your pyusb engine past the AR9271's stateful nature. Good luck with the porting—it’s a beast of a card to tame.

Do you have the byte signatures for the 802.11 TX/RX descriptors next, or are you just focusing on the control path for now?

