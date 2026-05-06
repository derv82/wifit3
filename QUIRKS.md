### `QUIRKS.md`

#### 1. The USB Control Endpoint is a Two-Way Street (READ vs. WRITE)
The most critical failure in early reverse-engineering attempts was treating the device initialization as a one-way firmware upload. 
* USB `bmRequestType 0x40` is a Host-to-Device **WRITE**.
* USB `bmRequestType 0xc0` is a Device-to-Host **READ**.
When hot-wiring baseband silicon, READs are not just informational—they are active hardware triggers. Reading a status register clears internal interrupts and advances the chip's state machine. If you strip the READ commands from your replay script and only blast WRITEs, the chip's internal latches get stuck, the voltage watchdogs time out, and the radio silently kills its own power. You must strictly interleave `0x40` and `0xc0` commands exactly as the native driver did.

#### 2. Register Blasting vs. Firmware Voodoo
Unlike modern Atheros or Mediatek chips that expect a massive, contiguous binary firmware blob (`.bin` file) uploaded via bulk endpoints, older chips like the RTL8187 operate entirely on "Register Blasting." The initialization sequence consists of ~8,000 individual control endpoint transfers (1-byte or 4-byte payloads). This includes manually bit-banging the Zebra RF synthesizer tuning tables. 

#### 3. The Configuration Reset Ignition Switch
Blindly filtering a `.pcap` for Vendor-specific commands (`bRequest == 5`) will hide standard USB protocol commands that act as ignition switches. In the case of the RTL8187, the Linux driver issues a standard `SET_CONFIGURATION` (`bmRequestType 0x00, bRequest 9`) immediately before opening the RX floodgates to reset the data toggles and flush the pipes. If this is missed, the hardware ignores subsequent commands.

#### 4. The Timeline Trap (Idle Sleep)
When extracting an initialization sequence from a `.pcap`, the cutoff point is critical. If you stop extracting commands the moment the channel is tuned, you may capture the hardware going into an "Idle Sleep" state. The actual commands that strip the hardware MAC filter and enable the RX engines often happen milliseconds before the first actual data packet arrives. **Rule of thumb:** Extract commands continuously up to the exact timestamp of the first received 802.11 frame.

#### 5. The WinUSB FIFO STALL (Errno 19)
If the hardware bootstrap sequence takes several seconds (due to thousands of writes with micro-delays), the radio may turn on and start filling the USB Bulk IN endpoint with 802.11 frames *before* the Python script starts its asynchronous reading thread. This causes a buffer overflow, prompting Windows to STALL and halt the endpoint. When PyUSB attempts to read from it later, it throws `[Errno 19] No such device`. 
* **Fix:** Always execute `dev.clear_halt(endpoint_address)` immediately before spawning the listener thread to flush the choked buffer.

#### 6. Packet Parsing Dependencies (The UAC Problem)
Relying on massive libraries like Scapy or fragmented libraries like `dpkt` for basic 802.11 frame parsing is a trap. 
* Scapy's architecture violently probes the host OS (registry, WMI, Npcap drivers) just by importing a sub-module, triggering mandatory Administrator (UAC) prompts on Windows. 
* **Fix:** The hardware bypass provides raw 802.11 MAC headers starting exactly at byte 0. Extracting frame types, subtypes, and MAC addresses is reliably done using native Python bitwise operations and byte slicing (`fc = data[0]; frame_type = (fc >> 2) & 0x03`). No external dependencies required.

#### 7. Timing Delays (The Speed Limit)
The hardware requires physical settling time (charging capacitors, locking frequency synthesizers). Firing 8,000 commands sequentially with 0.0 seconds of delay will outpace the silicon. However, native driver delays are highly conservative. It is entirely possible to compress maximum delays (e.g., clamping any delay > 10ms down to 10ms) to beat the speed of native tools like `airmon-ng`, provided the baseband doesn't crash.
