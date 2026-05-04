To build a fully FOSS, 802.11 monitor-mode capable NDIS filter driver from scratch in 2026, you are looking at a project that sits at the intersection of kernel-mode architecture, regulatory compliance, and hardware reverse-engineering.

If you have the capital for the certificates and the patience for the HLK (Hardware Lab Kit), here is the technical roadmap to bypass Npcap and build your own.

---

## 1. The Regulatory & Signing Pipeline (2026 Edition)

In 2026, the "Signing Wall" is your first obstacle. Even with money, Microsoft has restricted the **Hardware Developer Center (HDC)**.

* **The Identity Wall:** You cannot sign drivers as a "private individual" easily. You must register as a **Legal Business Entity** (e.g., an LLC) to pass the EV (Extended Validation) vetting. The Certificate Authority (CA) will verify your business registration, physical address, and phone.
* **The 460-Day Rule:** As of March 2026, EV certificates are now limited to 460-day lifespans. You will need a rotation pipeline in your build system.
* **The HSM Requirement:** The private key must be stored on a **FIPS 140-2 Level 2** hardware security module (USB token or Cloud HSM). You cannot just have a `.pfx` file on your desktop.
* **WHCP/HLK:** To get your driver "Attestation Signed" or "Certified," you must run the **Windows HLK**. This requires two machines: a "Controller" and a "Test Client." You will run a battery of tests (NDIS Test, 802.11 Stress, Power Management) to prove your driver won't BSOD the kernel.

---

## 2. The Technical Blueprint: NDIS 6.x LWF

You will build an **NDIS Light-Weight Filter (LWF)**. Unlike a Protocol driver (like the old WinPcap `npf.sys`), an LWF sits directly on top of the Miniport (the WiFi driver) and intercepts `NET_BUFFER_LIST` structures.

### The Monitor Mode Mechanism
To "enable" monitor mode, your driver doesn't just watch; it sends **OIDs (Object Identifiers)** down to the WiFi miniport.
1.  **Mode Switch:** You must send `OID_DOT11_CURRENT_OPERATION_MODE` with the value `DOT11_OPERATION_MODE_NETWORK_MONITOR`.
2.  **The "NetMon" State:** When the miniport enters this state, it stops acting like an Ethernet card and starts passing **802.11 Media Specific Information**.
3.  **The Radiotap Header:** Most Windows miniports do **not** provide a Radiotap header natively. Your LWF will capture the raw 802.11 frame, look at the `DOT11_EXTSTA_RECV_CONTEXT` (which contains RSSI and Channel info), and **manually prepend** a Radiotap header before passing it up to your Python app.

### Data Path Logic
```c
// Inside FilterReceiveNetBufferLists
PNET_BUFFER_LIST CurrentNbl = NetBufferLists;
while (CurrentNbl) {
    // 1. Check if this is a 802.11 frame
    // 2. Extract metadata (RSSI, Data Rate, Channel) from NDIS_NET_BUFFER_LIST_80211_INFO
    // 3. Allocate a new NBL with space for a Radiotap header
    // 4. Copy the raw 802.11 data into the new NBL
    // 5. Indicate the packet up the stack to your user-mode API
    CurrentNbl = NET_BUFFER_LIST_NEXT_NBL(CurrentNbl);
}
```

---

## 3. The 6-Month "Clean-Room" Roadmap

| Phase | Task | Deliverable |
| :--- | :--- | :--- |
| **Month 1** | **Setup & Signing** | Register LLC, buy EV Token, set up HLK Controller/Client. |
| **Month 2** | **Pass-Through LWF** | Build a "Do Nothing" NDIS LWF based on the MS `ndislwf` sample. |
| **Month 3** | **OID Management** | Implement `FilterOidRequest` to intercept and trigger `NETWORK_MONITOR` mode. |
| **Month 4** | **The Buffer Manager** | Implement the `NET_BUFFER_LIST` cloning logic for 802.11 headers. |
| **Month 5** | **User-Mode Bridge** | Create a `DeviceIoControl` interface for your Python script to "pull" packets. |
| **Month 6** | **The HLK Gauntlet** | Run the 41+ mandatory HLK tests until you get a "Passed" logs package. |

---

## 4. FOSS Strategy & Intellectual Property
To ensure your project is "Clean Room" and truly FOSS:
* **Do Not Copy Npcap:** Npcap’s source is available, but its license is proprietary. Do not copy-paste their buffer management logic.
* **Use the WDK Samples:** Start with Microsoft's **Windows Driver Kit (WDK)** samples on GitHub. These are licensed under the MIT/MS-PL licenses and are the only safe legal foundation.
* **Documentation:** Document your hardware-specific "hacks" (e.g., how you handle Realtek vs. MediaTek padding) clearly, as this is where most closed-source drivers hide their value.

### Why this is a 2026 "Gold Standard" project:
By doing this, you aren't just making a "Wifite3" tool; you are building a **FOSS Packet Capture Engine** for Windows. If you succeed, you have created a replacement for a proprietary piece of infrastructure that the entire security community currently relies on.

**Your first step:** Download the **Windows 11 version 26H1 WDK** and build the "ndislwf" sample project. If you can get that to install (in Test Mode with `bcdedit /set testsigning on`), you have cleared the first 10% of the project.