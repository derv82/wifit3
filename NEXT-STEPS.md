# Wifit3 Current Staus & Next Steps

## Current State: MISSION ACCOMPLISHED (CORE)
We have successfully implemented fully functional Userspace Python drivers for:
* [Atheros AR9271](./src/wifit3/chips/ar9271/) (v1.4).
* [Alfa/Realtek RTL8187](./src/wifit3/chips/rtl8187/).
* [Ralink RT5572 (rt2800usb)](./src/wifit3/chips/rt5572/).

The drivers can cold-boot, warm-boot, tune to channels, inject and sniff live 802.11 management frames.

And the UI is amazing, shows progress while intializing wireless cards, scanner view, "Focus" view all work on all 3 cards.

That said, we ONLY support 3 cards. And there's absolutely no actual "attacks" implemented yet. Except handshake capture (we detect, but we have no way to save a .pcap or .pcapng).

## NEXT STEP: HARDWARE SUPPORT

### Immediate Harware Support: RT5372 & RT3572

* Should be VERY similar to the RT5572 (already implemented).
* Sequencing Scripts: Firmware extraction, Boot sequence extraction, Channel Hopping sequence extraction.
* USB Transport layer.
* Driver:
  - Device boot init, Firmware uploading
  - Channel hop
  - 80211 frame parsing
  - Packet injection TX (deauth)
* WlanInterface & WlanDeviceManager integration

### Future Hardware Support (en-route)

- AC1900      (RTL8814AU)
- AWUS036AXML (MT7921AU)
- AWUS036ACH  (RTL8812AU)
- AWUS036ACHM (MT7610U)
- AWUS036ACM  (MT7612U)
- AWUS036ACS  (RTL8811AU)
- AWUS036NH   (RT3070) -> same chipset the older PAU05 uses.
- TP-Link Archer T3U Plus (RTL8812BU)

### Hardware to order (maybe)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU)
- *[Generic]* (MT7601U) -> Cheapest dongle, "Hello World" of wifi cards, broke/weird packet injection.
  - Buy: Just search "MT7601U" on eBay or Amazon; they are the tiny ones with a blue LED.

### NEXT STEP: Attack Stack (engine)

No actual functioning attacks (yet).

We want to avoid WEP as it's outdated. But all other attacks that Wifite2 can do now we should be able to do.

* 4-way Handshake capture (via client deauth)
  - Indicate when we have a handshake in the "Focus" UI view.
  - Auto-save handshakes to ./handshakes/? (current directory)? Or prompt user where to save (dialog window).
* PMKID extraction (see `hcxdumptool` and [this page](https://hashcat.net/forum/thread-7717.html) for details)
  - Could be a massive undertaking.
  - It looks like hcxdumptool just has chipset-specific optimizations for capturing packets... But there might be more than it's doing.
  - We'll need to support saving .pcapng-formatted captures so hashcat can utilize them.
  - We could start with a bare-bones PMKID extractor then dive into hcxdumptool to see what it's doing differently.
* WPS & Pixie Dust
  - WPS detection at the packet level (shown in Scanner & Focus views).
  - Bully/Reaver-style PIN brute forcing with backoff on rate limits, precise ETAs (could take days/weeks).
  - PixieWPS: Port the logic to Python (could be massive), Tracks vendor-specific "bad RNG" for generated E-hashes. Cracks known vendors with bad RNG in seconds/minutes.
* WPA3 downgrade attack (when in transition mode) -- this is just forcing a WPA2 4-way handshake right?
* WPA3 SAE crackable Group Numbers (19, 20, 22-24).
  - Need to better understand what this is before implementing ("i have no idea what's going on").
  - It seems like we just send 1 association frame to the AP, and it responds with a "REJECT" if that group number isn't supported -- we can just sniff these response frames to identify what group numbers the AP is susceptible to.
  - wifite2 uses wpa_supplication to attempt authenticating with the AP, I think 1 frame injection outta do it?
* Evil Twin (selecting 2nd interface).
  - Never used this, I'm not sure how effective these attacks are.

## NEXT STEP: MAC Vendor Identification

Database of vendor IDs (first 6 bytes of MAC) -> Vendor Name. Pretty sure wifite2 has this built in: https://github.com/kimocoder/wifite2/blob/HEAD/tools/fetch_oui.py
* Store in SQLite DB? That many strings would blow up the memory usage so hard.
* "Manufacturer" column in Scanner view shows "ASUS", "Apple", "Motorola", "Linksys", etc.
* List of clients (in "Focus" window) could also have manufacturer (Apple, Nintendo, Android, Dell) with Icons maybe?

## NEXT STEP: "Iconify the scanner"

**Note:** None of this is set in stone, just ideas, screen real estate is more important than fancy UIs with "power icons".

Since we are using Rich and Textual, we have several tiers of icons available:

1. Standard Emojis: You can use any standard emoji tag (e.g., :locked:, :shield:, :satellite_antenna:). Rich has a massive built-in library.
2. Unicode Symbols: Direct unicode characters work great and are very lightweight.
    * Signal Bars: ▂ ▄ ▆ █ (e.g., [green]▆[/green][dim]█[/dim])
    * Security: 🔒 (Encrypted), 🔓 (Open), ⚡ (WPA3/Fast)
    * Activity: 📡 (Beaconing), 🛰️ (Probing)
3. Box Drawing & Blocks: Great for custom meters or progress bars (e.g., ░▒▓█).
4. Nerd Fonts: Requires users install custom fonts in their Terminal, breaks over SSH probably, not a fan.

We could eventually replace the "ENC" text with a 🔒 icon and use vertical block characters (▂▄▆█) for the "PWR" column to make it look much more modern.


## Architectural Guidelines
*  **Lead's Rule:** Discuss class design (e.g., `GenericDriver` vs `WlanInterface` responsibilities) BEFORE execution. Treat the user as the Senior Lead.
