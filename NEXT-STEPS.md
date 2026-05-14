# Wifit3 Current Staus & Next Steps

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented fully functional Userspace Python drivers for:
* [Atheros AR9271](./src/wifit3/chips/ar9271/) (v1.4).
* [Alfa/Realtek RTL8187](./src/wifit3/chips/rtl8187/).
* [Ralink RT5572 (rt2800usb)](./src/wifit3/chips/rt5572/).

The drivers can cold-boot, warm-boot, tune to channels, inject and sniff live 802.11 management frames.

## NEXT STEPS

### Immediate Harware Support: RT5372 & RT3572

* Sequencing Scripts: Firmware extraction, Boot sequence extraction, Channel Hopping sequence extraction.
* USB Transport layer.
* Driver:
  - Device boot init, Firmware uploading
  - Channel hop
  - 80211 frame parsing
  - Packet injection TX (deauth)
* WlanInterface & WlanDeviceManager integration

### UI Design: Attack Mode

Brainstorm / design UI/UX during attack sequences. Open questions:

* How will user select a target / client? How will the interface change?
  - Can the user target multiple access points at once? (Probably not? Different channels...)
  - User "focuses" on a target,
  - UI changes to "AP Detail" view,
  - Changes wireless card channel to match AP,
  - Shows AP clients,
  - Shows relevant attack options (WPA2/WPA3/WPS).
* How will Wifit3 capture packets?
  - In the main 'scanning' loop, or a separate scan during attack?
  - Write to file by-default or only when requested by user?
* How will user "start", "pause", or "interrupt/stop" an attack?
* Can the user run multiple simultaneous attacks against a single access point? (E.g. handshake+death & pixiedust)

### Attack Stack (engine)

* 4-way Handshake capture (via client deauth)
* PMKID extraction (see `hcxdumptool` and [this page](https://hashcat.net/forum/thread-7717.html) for details)
* WPS Pixie Dust (WPS detection, Bully/Reaver-style brute forcing, Pixiewps?)
* Evil Twin (selecting 2nd interface)
* Other attacks?

### Future Hardware Support (en-route)

- AC1900      (RTL8814AU)
- AWUS036AXML (MT7921AU)
- AWUS036ACH  (RTL8812AU)
- AWUS036ACHM (MT7610U)
- AWUS036ACM  (MT7612U)
- AWUS036ACS  (RTL8811AU)
- AWUS036NH   (RT3070) -> same chipset the older PAU05 uses.
- TP-Link Archer T3U Plus (RTL8812BU)

#### Hardware to order (maybe)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU)
- *[Generic]* (MT7601U) -> Cheapest dongle, "Hello World" of wifi cards, broke/weird packet injection.
  - Buy: Just search "MT7601U" on eBay or Amazon; they are the tiny ones with a blue LED.

## Architectural Guidelines
*  **Lead's Rule:** Discuss class design (e.g., `GenericDriver` vs `WlanInterface` responsibilities) BEFORE execution. Treat the user as the Senior Lead.
