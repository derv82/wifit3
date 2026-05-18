# Wifit3 Current Staus & Next Steps

## Current State: Supported Chipsets

Fully-functional userspace Python drivers (cold + warm bring-up, channel hop, inject + sniff, integrated with the TUI):

| Card / Family | Driver | Bands | Status |
|---|---|---|---|
| Atheros AR9271 | `chips/ar9271/` | 2.4 GHz | DONE (v1.4) |
| Alfa/Realtek RTL8187 | `chips/rtl8187/` | 2.4 GHz | DONE |
| Ralink RT2800USB (RT5572 / RT3572 / RT5372) | `chips/rt2800usb/` | 2.4 + 5 GHz (RT5572) | DONE |
| Realtek RTL8821AU (AWUS036ACS) | `chips/rtl8821au/` | 2.4 + 5 GHz | DONE 2026-05-17, 27 BSSIDs/8s on ch1 |
| Realtek RTL8822BU (TP-Link T3U Plus, AC1300) | `chips/rtl8822bu/` | 2.4 + 5 GHz, 2T2R | DONE 2026-05-17, full RX + TX inject + 5G |
| Realtek RTL8812AU (AWUS036ACH) | `chips/rtl8812au/` | 2.4 + 5 GHz, 2T2R | DONE 2026-05-17, RX + deauth confirmed by handshake re-capture |
| Mediatek MT7921AU (AWUS036AXML) | `chips/mt7921au/` (scaffold) | — | PAUSED — see [[MT7921AU.md]] (EP0 dies post-FW_START_REQ on WinUSB; blocked on libusb bump for RAW_IO support) |

Family-shared infrastructure under `chips/rtw88_base/` covers transport,
phy_cond walker, power_seq runtime, RF SIPI, TX checksum, RX-desc parser,
and the legacy MCUFWDL FW upload — both 88xxA chips (8821a + 8812a) and
the modern 8822b share through it.

Attack stack so far: handshake **detection** (the 4-way EAPOL pair is
parsed live), deauth inject (verified on 8812au by re-capturing handshake
after target client reconnect). Pcap save and PMKID extract not yet
implemented.

## Open work on RTL8812AU (the just-landed chipset)

Two follow-ups are tracked, both deferred but small:

1. **Queue-clear bisect** — `REG_RQPN / REG_RQPN_NPQ / REG_TXDMA_PQ_MAP`
   get silently cleared somewhere in `post_mac_init_phy`, working around
   it via `_arm_tx_queues` before each `inject_frame`. The bisect
   diagnostic landed in commit `2c7a465`; user runs `--debug` once and
   we see exactly which step does the clear. Pin down root cause +
   remove the workaround.
2. **EFUSE-read verification + sensitivity confirmation** — EFUSE-read
   landed in `71699d7`. Awaiting hw test to confirm:
   (a) EFUSE values readable on AWUS036ACH (rfe_option, ext_lna, etc.),
   (b) feeding them into the bring-up actually fixes the "only-NETGEAR2G"
   sensitivity gap from earlier testing.

## NEXT STEP: M-LAST and 1.0

Per [[project-m-last-libusb-bump]] memory: bump `libusb_package` to
≥1.0.27 + enable `LIBUSB_OPTION_WINUSB_RAW_IO` to unblock MT7921AU on
Windows. That's the last chipset gating wifit3 1.0.

### Other hardware queued (when we get back to it)

- AC1900      (RTL8814AU) — 4T4R, modern iDDMA path. Bigger delta from 8822bu.
- AWUS036ACHM (MT7610U)
- AWUS036ACM  (MT7612U)
- AWUS036NH   (RT3070) — same chipset the older PAU05 uses.

### *Distant Future* Hardware Support (need $$$ will make more Minnie Drivers)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU)
- *[Generic]* (MT7601U) -> Cheapest dongle, "Hello World" of wifi cards, broke/weird packet injection.
  - Buy: Just search "MT7601U" on eBay or Amazon; they are the tiny ones with a blue LED.

## NEXT STEP: Attack Stack Engine

No actual functioning attacks (yet).

We want to avoid WEP as it's outdated. But all other attacks that Wifite2 can do now we should be able to do, natively in Python.

* 4-way Handshake capture (via client deauth)
  - We already detect the handshake packets in the WlanManager.
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
  - See [WPA3-SAE-Group-Detection.md](./WPA3-SAE-Group-Detection.md).
  - Need to better understand what this is before implementing ("i have no idea what's going on").
  - It seems like we just send 1 association frame to the AP, and it responds with a "REJECT" if that group number isn't supported -- we can just sniff these response frames to identify what group numbers the AP is susceptible to.
  - wifite2 uses wpa_supplication to attempt authenticating with the AP, I think 1 frame injection outta do it?
* Other WPA3 notes in [WPA3-Frames.md](./WPA3-Frames.md).
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
