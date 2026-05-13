# Handoff: Wifit3 AR9271 Driver - Intelligence & Integration

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented [a fully functional Userspace Python driver for the Atheros AR9271](./src/wifit3/chips/ar9271/) (v1.4). The driver can cold-boot, calibrate, tune to channels, and sniff live 802.11 management frames. Likewise for [Alfa/Realtek's RTL8187](./src/wifit3/chips/rtl8187/).

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
