# Handoff: Wifit3 AR9271 Driver - Intelligence & Integration

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented fully functional Userspace Python drivers for:
* [Atheros AR9271](./src/wifit3/chips/ar9271/) (v1.4).
* [Alfa/Realtek RTL8187](./src/wifit3/chips/rtl8187/).
* [Ralink RT5572 (rt2800usb)](./src/wifit3/chips/rt5572/).

The drivers can cold-boot, calibrate, tune to channels, and sniff live 802.11 management frames.

## NEXT STEPS

### RT5372 & RT3572

* Sequencing Scripts: Firmware extraction, Boot sequence extraction, Channel Hopping sequence extraction.
* USB Transport layer.
* Driver:
  - Device boot init, Firmware uploading
  - Channel hop
  - 80211 frame parsing
  - Packet injection TX (deauth)
* WlanInterface & WlanDeviceManager integration

## Architectural Guidelines
*  **Lead's Rule:** Discuss class design (e.g., `GenericDriver` vs `WlanInterface` responsibilities) BEFORE execution. Treat the user as the Senior Lead.

## Upcoming Hardware Deliveries

- AC1900      (RTL8814AU)
- AWUS036AXML (MT7921AU)
- AWUS036ACH  (RTL8812AU)
- AWUS036ACHM (MT7610U)
- AWUS036ACM  (MT7612U)
- AWUS036ACS  (RTL8811AU)
- AWUS036NH   (RT3070) -> same chipset the older PAU05 uses.
- TP-Link Archer T3U Plus (RTL8812BU)

## Need to oroder / Consider ordering

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU)
- [Generic] (MT7601U) -> Cheapest dongle, "Hello World" of wifi cards, broke/weird packet injection.
  - Buy: Just search "MT7601U" on eBay or Amazon; they are the tiny ones with a blue LED.
