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
| Realtek RTL8188EUS (TP-Link TL-WN722N v2/v3) | `chips/rtl8188eus/` | 2.4 GHz, 1T1R | DONE 2026-05-19, M1-M8 complete; passive 4-way handshake + active PMKID harvest verified live |
| Mediatek MT7921AU (AWUS036AXML) | `chips/mt7921au/` (scaffold) | — | PAUSED — see [[MT7921AU.md]] (EP0 dies post-FW_START_REQ on WinUSB; blocked on libusb bump for RAW_IO support) |

Family-shared infrastructure under `chips/rtw88_base/` covers transport,
phy_cond walker, power_seq runtime, RF SIPI, TX checksum, RX-desc parser,
and the legacy MCUFWDL FW upload — both 88xxA chips (8821a + 8812a) and
the modern 8822b share through it.

Attack stack so far: handshake **detection** (the 4-way EAPOL pair is
parsed live, M1-M4 classified from Key Info bits, per-client so
simultaneous clients never overwrite each other), **per-AP capture
display + dual save** in Focus view (writes both a linktype-105 libpcap
AND a hashcat-native `.hc22000` hashline file — no `hcxpcapngtool`
needed), deauth inject (verified on 8812au by re-capturing handshake
after target client reconnect). PMKID extract not yet implemented — the
model has a forward-compat `Handshake.pmkid` slot and the UI displays
its status, but the actual Auth/Assoc-Req injection + RSN-IE PMKID
parse is still TODO (once it lands, the PMKID hashline path is already
wired through `engine/hc22000.py`).

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
  - We already detect the handshake packets in the WlanManager. ✅
  - Indicate when we have a handshake in the "Focus" UI view. ✅ (CAPTURE
    column + per-client Handshake column + EVENT LOG, all live-updating;
    pair validity correctly checked via M1/M2/M3/M4 classification, not
    just replay-counter grouping).
  - ✅ Save button writes BOTH `./captures/<ssid>_<bssid>_<ts>.pcap`
    (libpcap, beacon + all EAPOL frames) AND `<...>.hc22000` (hashcat
    mode-22000 hashlines — eliminates the `hcxpcapngtool` dependency).
  - **Open: dynamic channel re-steering.** If the AP CSA-jumps to another
    channel or shows stronger signal elsewhere (multi-band AP advertising
    on both 2.4 and 5), Focus stays glued to whatever channel was current
    on entry. Future QoL: periodically probe nearby channels (<100 ms
    each) and re-tune if the AP's beacon rate/RSSI is higher there.
    Likely tied to ESSID-based targeting (one logical AP can have multiple
    BSSIDs across bands).
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

## Pre-release ETHICS / SAFETY CHECKLIST

Before any of this ships to users beyond ourselves, the following must
be addressed. Listed in roughly increasing scariness.

### License compliance for ported drivers

Wifit3's chip drivers are cleanroom ports of GPL-2.0 (or later) kernel
code. Our Python is original work — we re-derive register sequences
from the kernel C — but the line between "inspired by" and "derived
work" can be blurry. To stay clearly on the right side:

- **Every chip driver should carry a SPDX header**: `SPDX-License-Identifier: GPL-2.0-or-later` (or whatever the upstream chose) plus an attribution like `Ported from Linux rtl8xxxu (kernel v6.18) by <author>, <year>`. Currently we have no SPDX on any of `chips/<name>/*.py`. Fix before any wider release.
- **The `data_dumps/<driver>-source-v6.18/` directories** are reference material — pristine kernel source from kernel.org. They're not committed but the path is in our gitignore and `CLAUDE.md`. `[SRC]` citations in our code reference these files by path. That referencing is fine (citing a Linux kernel file in a comment is universal). What's NOT fine:
  - The user noted they stripped copyright headers from local copies of `<driver>-source-v6.18/` to save context. **Those local edits are personal-workspace-only — never published, never in git**. The kernel's COPYING / GPL terms apply to the upstream files, not to our local annotations of them. Anyone trying to reproduce the citations needs to pull pristine sources from kernel.org themselves (instructions in CLAUDE.md or a CONTRIBUTING.md).
  - **Action: add `CONTRIBUTING.md`** explaining the cite-by-path convention + how to fetch the referenced kernel source pristinely from kernel.org. So a reviewer following a `[SRC] core.c:5321` citation has a clear path to the upstream original.
- **`assets/<firmware>.bin` files** ship in-repo. These are pcap-extracted blobs that are byte-identical to `linux-firmware`'s files. linux-firmware has its own per-file license (`linux-firmware/WHENCE` documents them — usually a Realtek/Atheros/Mediatek redistribution license). Action: confirm the redistribution terms for each shipped FW blob and include the original LICENSE.* alongside.
- **Init tables** (`phy_tables.py` etc) are re-encodings of GPL'd `const u32` arrays. The encoding (Python tuple of tuples) is mechanical and probably not a creative work — but the underlying data IS the kernel's. SPDX header + attribution covers this. **Don't strip the kernel's per-file attributions from comments in our Python ports**.

### Hardware safety — preventing burnouts / fires

`set_tx_power`, `init_phy_*`, and the firmware payloads physically drive analog circuits on the dongle. Bugs in these paths can:
- Drive the PA out of its rated power envelope (overheating + accelerated aging — months/years compressed into days)
- Leave a register in a stuck-active state that draws idle current continuously (warm chip = small, sustained heat — could degrade nearby plastic, in worst case start a fire if a passive component fails open)
- Skip a power-off sequence on close, leaving the chip drawing 100mW indefinitely with no thermal feedback loop

Pre-release safety bar:

- **Extended stress testing per supported chipset.** Per driver, run `--phase all` followed by 30-minute beacon-capture + 30-minute TX-inject loops continuously. Touch-test temperature at 5/10/20/30-minute marks. ANY chip running hotter than "warm bath" should pause the test pending investigation. Document the test in a per-chip `<CHIP>-stress.md`.
- **Read thermal sensor where available.** rtl8xxxu has `REG_THERMAL_METER` and a `RF6052_REG_T_METER`; rtw88 has on-chip thermal that exposes via `priv->fops->thermal_meter`; mt76 has DPD/thermal C2H events. **For every supported chipset, periodically poll the thermal sensor during normal operation and log warnings if it exceeds a per-chip threshold.** Default: warn at 60°C, kill the driver at 75°C. Threshold sourced from the chip datasheet.
- **Pre-release line-by-line audit.** Walk the kernel source side-by-side with our port, looking for: (1) writes we made that the kernel doesn't, (2) writes the kernel makes that we omitted, (3) magic values we typo'd. Flag everything. Even "looks fine" needs a justifying comment. This is `[[port-full-helper]]` and `[[port-all-cases]]` applied with paranoia for the final pass.
- **Power-off sequence on `close()`.** Each driver's `close()` must restore the chip to a power-saving state (REG_TXPAUSE = 0xFF, REG_RF_CTRL clear, etc.) — NOT just release USB. Otherwise we leave a hot chip drawing current until the user unplugs.

### Removing the Custom TX power override entry entirely

Per user 2026-05-19: shelved indefinitely. It's a footgun ("jailgun"). Even with the yellow banner, the asymmetry between "easy to enable" and "hard to undo if you damaged a neighbor's wifi or your own PA" is too steep. If a user genuinely needs +30 dBm output for lab work, they can fork wifit3 and write the override themselves, owning that choice fully. Not a feature we ship.

## *Distant Future*: Configurable TX power override

**SHELVED 2026-05-19.** See ETHICS / SAFETY CHECKLIST above. We're not building this. The chip physically supports it; we don't surface it. If a researcher genuinely needs it for lab work in an RF cage, forking is the right path — owning that choice fully is part of the responsibility.

Keeping the sketch below for the day someone asks again and we need to point to the explicit decision:

Each driver currently uses TX power values populated from EFUSE during
chip bring-up (regulatory-compliant — same numbers the kernel uses).
The rtl8xxxu / rtw88 / mt76 silicon physically supports power indices
well above the EFUSE-burned regulatory caps; since wifit3 runs in
userland and bypasses the kernel's regulatory clamping, a runtime
override knob is technically straightforward.

What's NOT straightforward:

- Per-driver constants tables that vary wildly across families
  (rtl8xxxu uses 6-bit indices into REG_TX_AGC_* curves; rtw88 has
  txagc_set tables; mt76 uses different MCU commands entirely).
- Each chip's power amplifier has different headroom — the same "index
  0x3F" means different dBm on each chipset.
- Regulatory + SAR + PA-lifetime caveats need clear UX (a single
  `--tx-power N` flag that just blasts max on every supported card
  invites real-world harm).

Sketch when we get there:

- Per-driver `set_tx_power_override(index_or_dbm)` method, optional
  (drivers without a supported PA bypass return False).
- A driver-side calibration table mapping "user wants N dBm" → "register
  value that gets close on this chip".
- Big yellow banner the first time a session runs above the EFUSE cap,
  with a one-line confirmation. Default = EFUSE values.
- Ideal: a Bolivia-style regdomain hack via a userland `iw reg set`
  equivalent (we'd write the country code to the relevant chip register
  and let the firmware's per-country table do the clamping for us). This
  may not be possible on all chips — the regulatory enforcement lives
  partly in the EFUSE on some silicon and can't be overridden post-burn.

Useful for: pen-testing labs (RF cage, deliberately weak SNR scenarios),
long-distance test setups, or "I can see my AP's beacons but my deauth
isn't reaching it" workarounds. NOT useful for: anything within shared
RF spectrum where neighbors exist.

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
