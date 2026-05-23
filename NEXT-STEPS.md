# Wifit3 Current Staus & Next Steps

## Current State: Supported Chipsets

Fully-functional userspace Python drivers (cold + warm bring-up, channel hop, inject + sniff, integrated with the TUI):

| Card / Family | Driver | Bands | Status |
|---|---|---|---|
| Atheros AR9271 (AWUS036NHA) | `chips/ar9271/` | 2.4 GHz, 1T1R | DONE 2026-05-22 — cleanroom-FW RX protocol fully RE'd (36-B header, rs_status + FCS gating, kernel-faithful HIF reassembler); PMKID + EAPOL M1-M4 hw-verified |
| Alfa/Realtek RTL8187 | `chips/rtl8187/` | 2.4 GHz | DONE |
| Ralink RT2800USB (RT5372 / RT5572) | `chips/rt2800usb/` | 2.4 GHz (RT5372 1T1R); 2.4 + 5 GHz (RT5572 2T2R) | DONE 2026-05-20 — RT5372 + RT5572 (silicon RT5392/RT5592) hw-verified for scan + monitor + TX inject. RT3572 split out to known-broken below. |
| Mediatek MT7612U (AWUS036ACM, Alfa) | `chips/mt76x2u/` | 2.4 + 5 GHz, 2T2R | DONE 2026-05-20 — full attack stack hw-verified; deauth on NETGEAR2G recaptured EAPOL M1+M3 live |
| Mediatek MT7610U (AWUS036ACHM, Alfa) | `chips/mt76x0u/` | 2.4 + 5 GHz, 1T1R | DONE 2026-05-22 — M1→full attack stack landed in ~24 h (commits `49df6f4`..`b887282`); PMKID capture hw-verified by user. mt76 family, single-chain WiFi-5 sibling of MT7612U. |
| Realtek RTL8821AU (AWUS036ACS) | `chips/rtl8821au/` | 2.4 + 5 GHz | DONE 2026-05-17, 27 BSSIDs/8s on ch1 |
| Realtek RTL8822BU (TP-Link T3U Plus, AC1300) | `chips/rtl8822bu/` | 2.4 + 5 GHz, 2T2R | DONE 2026-05-17, full RX + TX inject + 5G |
| Realtek RTL8812AU (AWUS036ACH) | `chips/rtl8812au/` | 2.4 + 5 GHz, 2T2R | DONE 2026-05-17, RX + deauth confirmed by handshake re-capture |
| Realtek RTL8188EUS (TP-Link TL-WN722N v2/v3) | `chips/rtl8188eus/` | 2.4 GHz, 1T1R | DONE 2026-05-19, M1-M8 complete; passive 4-way handshake + active PMKID harvest verified live |
| Mediatek MT7921AU (AWUS036AXML) | `chips/mt7921au/` (scaffold) | — | PAUSED — see [[MT7921AU.md]] + [[KALI-HANDOFF-2026-05-19.md]] (kernel-driver-collision confirmed + fixed via blacklist+rmmod+replug; 2026-05-19 evening Kali re-run got past PATCH_SEM_GET and uploaded all firmware, but **FW_START_REQ wall reproduces on Kali too** — not WinUSB-specific. Leading hypothesis: shallow bulk-IN URB pool — needs libusb async URB queue) |

Family-shared infrastructure under `chips/rtw88_base/` covers transport,
phy_cond walker, power_seq runtime, RF SIPI, TX checksum, RX-desc parser,
and the legacy MCUFWDL FW upload — both 88xxA chips (8821a + 8812a) and
the modern 8822b share through it.

## Known broken / partial-support cases

Cross-card PMKID + SAE Probe verification on 2026-05-22 surfaced two
chip-specific issues that aren't covered by the per-chipset rows above.
Tracked here so they don't fall on the floor between sessions:

- **Ralink RT3572 (AWUS051NH v2) — TX RF-silent on Windows, paused
  pending Kali usbmon diff.** All digital indicators say the chip is
  transmitting (TX_STA_FIFO entries with TX_SUCCESS=1, TX_STA_CNT1
  counter increments, bulk-OUT URBs accepted) but a known-good sniffer
  card (AWUS036ACS) 5cm away on the same channel sees **zero deauths
  on-air** during a 40-frame burst. Same hardware works under the Kali
  kernel driver (`aireplay-ng --test` shows "Injection is working").
  Conclusively: chip's analog/RF stage isn't emitting in our driver.

  Investigation 2026-05-22 found and fixed several real issues along
  the way that should land regardless of whether RT3572 ever works:
    - TXWI now matches kernel byte-for-byte (TX_OP=HT_TXOP_NONE,
      WCID=0, NSEQ=0, PACKETID_QUEUE=0, PACKETID_ENTRY=2). Previously
      we used 0xFF/1/2/1 which on rtw88 and mt76 chips works but on
      rt2800 the chip's TX engine treats `TX_OP=HT_TXOP_RTS` as
      "do RTS/CTS first" — silent drop for spoofed-srcMAC unicast.
    - Inject endpoint switched from EP 0x02 (AC_VI) to EP 0x01 (AC_VO)
      to match where the kernel sends mgmt frames.
    - `TX_SW_CFG0` made silicon-specific in `init_registers`: kernel
      writes 0x400 for RT3572, 0x404 for RT5390/RT5392, 0x404 for
      RT5592. We were hardcoding 0x404 for all silicons — undocumented
      vendor-magic register.
    - WCID + WCID_ATTR table now cleared per kernel pattern (256× 0xFF
      for WCID entries via `memset(0xFF)`, 256× 0 for ATTR). Previously
      not cleared at all — chip's reset state left CIPHER bits with
      possibly-non-zero RAM contents.
    - `MCU_WAKEUP` (cmd 0x31, arg1=2) sent before `enable_radio` to
      match `rt2800usb_set_device_state(STATE_RADIO_ON)`.
    - `TX_PWR_CFG_0..4` per-rate TX power tables now written (kernel
      sources from EEPROM_TXPOWER_BYRATE; we fall back to 0x0A per
      4-bit rate field on unburned EFUSE).
    - `txpath/rxpath` forced to 2 for RT3572 when EFUSE is unburned
      (NIC_CONF0=0x0000) so `init_bbp_3572` keeps DAC1 powered.

  None of those fixes restored on-air RF. Next step: run wifit3 on
  Kali to rule out Windows/WinUSB vs driver. If Kali also fails,
  `usbmon` diff between `aireplay-ng --test` (works) and wifit3 (fails)
  will expose the missing register write or sequence.
- **Mediatek MT7921AU** — separate, fully detailed below in
  **NEXT STEP: MT7921AU**. Driver paused on FW_START_REQ wall.

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

## NEXT STEP: MT7921AU

**Plan revised 2026-05-19 evening** based on the post-blacklist Kali re-run
(bundles in `usb_dumps/wifit3-kali-bundle/run-2026051*`). The libusb-bump
+ `LIBUSB_OPTION_WINUSB_RAW_IO` work is **Windows-only** — and the FW_START_REQ blocker now reproduces on Kali +
libusb too, so that knob can't be the full unlock.

New leading hypothesis: **shallow bulk-IN URB pool.** Linux's
`mt76u_alloc_queues` pre-submits 128 URBs per IN endpoint before any
firmware traffic; our `transport.py` does one-at-a-time sync reads on a
drainer thread. The boot ROM appears to use USB-3 flow control across
both directions simultaneously, so if we're not in a posted state to
receive an internal ACK/event, the device stops accepting OUTs (which
matches both the FW_SCATTER 4-packet stall AND the FW_START_REQ EP0
death — both now reproduced on Kali).

Concrete plan for MT7921AU:

1. **Restructure `transport.py`** to use the libusb async URB API
   (`libusb_submit_transfer`) instead of `Endpoint.read()`. Pre-submit
   ~32 URBs each on EP 0x84 and EP 0x85 before the first MCU command,
   refill on completion. Likely involves dropping to `libusb1` direct
   from PyUSB.
2. **Hw-test on Windows first** — faster turnaround, the symptom
   (FW_START_REQ then EP0 dead) is well-characterised there.
3. **If Windows unblocks → confirm on Kali** with a clean
   blacklist+replug (use `scripts/kali_test_mt7921au.sh`, after fixing
   its tshark `Permission denied` bug).
4. **If Windows still fails** → re-derive from Linux's pcap how the
   kernel sequences URBs around FW_START_REQ. The libusb-bump +
   WINUSB_RAW_IO knob is still worth trying as a secondary, but is not
   the primary fix any more.

### Other hardware queued (captures landed 2026-05-19, on deck)

User trip to Kali brought back fresh cold-boot pcaps + `airmon-ng`/`iw`/`tshark`
logs for three of the four queued cards. Each capture lives at
`usb_dumps/captures_<driver>/` with the standard `capture-N.pcap` +
`capture-N_logs/main.log` layout the existing tooling already consumes
(`scripts/pcap_slicer.py`, plus `Grep`/`Read` against `data_dumps/<driver>-source-v6.18/`). Remaining gate
before driver work starts on any of these is **kernel source extraction
to `data_dumps/<driver>-source-v6.18/` + firmware-blob byte-verify against
`linux-firmware/`** (same workflow as the 8821au/8822bu/8812au bring-ups).

| Card | Chip | Kernel module | Captures | Source | FW | Notes |
|------|------|---------------|----------|--------|----|-------|
| AC1900       | RTL8814AU | `rtw88_8814au` | ✅ `captures_rtw88_8814au/` (3) | ⏳ | ⏳ | 4T4R, modern iDDMA path. Bigger delta from 8822bu — 4 RF paths instead of 2, larger txbf init. Family-shared `rtw88_base/` should still cover transport / phy_cond / power_seq / SIPI. |
| AWUS036ACM   | MT7612U   | `mt76x2u`      | ✅ `captures_mt76x2u/` (3) | ✅ | ✅ | **DONE 2026-05-20** — see `chips/mt76x2u/MT76X2U.md`. The "~2 s channel-switch breathing room" noted during capture turned out to be a **capture-host artifact, NOT a chip constraint** — our wifit3 driver does the full kernel-faithful 5-cal-MCU-CMD switch in ~150 ms (see `--phase hop` output). The `aireplay-ng` >10s latency was likely the same Kali-host effect. Defer `chips/mt76_base/` until MT7921AU revives — two impls aren't enough yet to know which seam to factor. |
| AWUS036ACHM  | MT7610U   | `mt76x0u`      | ✅ `captures_mt76x0u/` (3)      | ✅ | ✅ | **DONE 2026-05-22** — see commit history `49df6f4`..`b887282`. M1 (FW upload) through M6 (deauth + EAPOL) + PMKID/SAE landed in one push. PMKID capture re-verified by user post-decloak-session. Single-chain WiFi-5; defer `chips/mt76_base/` factoring (see MT7612U row above). |

Next mechanical steps when picking one of these up:

1. `pcap_slicer.py usb_dumps/captures_<driver>/capture-1_logs/main.log usb_dumps/captures_<driver>/capture-1.pcap` — get the frame-range map for "plug in → firmware load → channel hop → packets flow". Pick the cold-boot capture (usually capture-1 or -3; -2 tends to be a warm boot where the kernel skipped FW load).
2. Pull pristine kernel source for the relevant driver subdir into `data_dumps/<driver>-source-v6.18/` (kernel.org tag `v6.18`, same version as Kali's runtime kernel — keeps `Grep`/`Read` citations version-aligned).
3. Extract the firmware blob from frame N of the cold-boot pcap (mirror the `scripts/rtl8812au/extract_rtw8812a_fw.py` pattern already used for rtw88) and byte-verify against `linux-firmware/<driver>/*.bin`. Ship the pcap-extracted blob in `chips/<driver>/assets/` per [[firmware-extraction]] memory.
4. M1 = FW upload + FW_READY ACK only. Demoable, ~few hundred lines, no PHY init. Per [[milestone-sizing]] memory.

### *Distant Future* Hardware Support (need $$$ will make more Minnie Drivers)

- TP-Link Archer T2U Plus (RTL8821AU / RTL8811AU)
- *[Generic]* (MT7601U) -> Cheapest dongle, "Hello World" of wifi cards, broke/weird packet injection.
  - Buy: Just search "MT7601U" on eBay or Amazon; they are the tiny ones with a blue LED.
- AWUS036NH (RT3070) - Same chipset as the older PAU05 dongle. Should slot into the existing `chips/rt2800usb/` driver — likely a `DeviceID` + chip-id extras entry + minor RXWI/TXWI tweaks, not a from-scratch port.

## NEXT STEP: Attack Stack Engine

No actual functioning attacks (yet).

WEP attacks scoped in [`src/wifit3/engine/attacks/wep/README.md`](src/wifit3/engine/attacks/wep/README.md) — ARP-replay + MVP shell-out PTW first (~1 week), full-native PTW + fragmentation + chopchop second (~2-3 weeks). All other attacks that Wifite2 can do, we should be able to do natively in Python.

* 4-way Handshake capture (via client deauth)
  - **Open: dynamic channel re-steering.** If the AP CSA-jumps to another
    channel or shows stronger signal elsewhere (multi-band AP advertising
    on both 2.4 and 5), Focus stays glued to whatever channel was current
    on entry. Future QoL: periodically probe nearby channels (<100 ms
    each) and re-tune if the AP's beacon rate/RSSI is higher there.
    Likely tied to ESSID-based targeting (one logical AP can have multiple
    BSSIDs across bands).
* PMKID extraction (see `hcxdumptool` and [this page](https://hashcat.net/forum/thread-7717.html) for details)
  - We landed PMKID extraction, wired it into the UI, it works well.
  - It appears hcxdumptool has chipset-specific optimizations for capturing packets... But there might be more that it's doing.
* WPS & Pixie Dust
  - WPS detection at the packet level (shown in Scanner & Focus views).
  - Bully/Reaver-style PIN brute forcing with backoff on rate limits, precise ETAs (could take days/weeks).
  - PixieWPS: Tracks vendor-specific "bad RNG" for generated E-hashes. Cracks known vendors with bad RNG in seconds/minutes. TODO: Port the logic to Python (could be massive), 
* WPA3 downgrade attack (when in transition mode) -- implemented by responding to probe requests.
* WPA3 SAE crackable Group Numbers (19, 20, 22-24).
  - See [WPA3-SAE-Group-Detection.md](./WPA3-SAE-Group-Detection.md).
  - We have added SAE Group Number enumeration; have not verified the numbers are accurate.
* Other WPA3 notes in [WPA3-Frames.md](./WPA3-Frames.md).
* Evil Twin (selecting 2nd interface).
  - Never used this, I'm not sure how effective these attacks are.

## *Distant Future*: Configurable TX power override

**SHELVED 2026-05-19.** We're (probably) not building this. The chip physically supports it; we don't surface it. If a researcher genuinely needs it for lab work in an RF cage, forking is the right path — owning that choice fully is part of the responsibility.

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

## NEXT STEP: User Persistence + Decloak DB

**Scope:** likely an entire session of work — covers app config, user preferences, and a long-lived BSSID→SSID mapping. Three distinct concerns that should share a storage layer.

### 1. Persistent user config

Settings the user changes during a session and would want to keep across runs:
* Theme selection (currently hardcoded to `textual-dark` in `ui/app.py`)
* Default sort column + direction in Scanner (`_sort_idx`, `_sort_reverse`)
* Fade toggle state + fade-duration constants if we ever make them configurable
* `hashcat` binary path (for future "crack handshake" wiring)
* Capture output directory (currently hardcoded to `captures/`)
* Default channel filter (e.g. "I only care about 2.4 GHz unless I say so")

### 2. Decloaked SSID database

When a hidden AP gets decloaked via a Probe Response carrying the real SSID, we currently log the mapping to the system-log and then *lose it on app exit*. Should persist.

Schema sketch:
```
bssid TEXT PRIMARY KEY
ssid TEXT
first_seen TIMESTAMP
last_seen TIMESTAMP
sighting_count INTEGER     -- # of distinct beacon/probe-response captures
confidence FLOAT           -- 0..1, see below
sources INTEGER            -- bitmask: 1=beacon-decloak, 2=probe-response, 4=manual
```

**Confidence counter** — defense against MDK3-style probe-response spam. If we see `bssid=AA:BB:CC:11:22:33 → ssid="totally-real-WiFi"` exactly once, confidence is low. If we see the same mapping consistently over hundreds of frames across multiple sessions, confidence is high. Cap at 1.0. Decay slowly on conflicting evidence (e.g. same BSSID claiming a different SSID).

UI integration: when the scanner sees a "hidden" AP whose BSSID matches a high-confidence entry, render the stored SSID with a `(decloaked)` muted suffix and italic style. Low-confidence entries shown with a `?` suffix to flag them as speculative.

### 3. Storage layer

* **Format:** SQLite (one DB, multiple tables: `config`, `decloaked_ssids`, future `oui_overrides`). Lightweight, atomic writes, fast lookups.
* **Location:** XDG-compliant on Linux (`~/.config/wifit3/wifit3.db`), platform-native on Windows (`%APPDATA%/wifit3/wifit3.db`). Use `platformdirs` for cross-platform resolution.
* **Migration:** version the schema from day one (`PRAGMA user_version`), expect to alter as features land.
* **Privacy note:** the decloak DB is a passive sniffing artifact — document clearly in ETHICS/SAFETY checklist that the file contains evidence of nearby networks the user has been in range of.

### Open questions
* Should config live in TOML (human-editable) with only the decloak DB in SQLite? Possibly cleaner separation.
* Auto-prune old decloak entries (e.g. drop entries unseen for >90 days), or grow forever?
* When user runs wifit3 on a different machine — does the DB roam? (Probably not; treat as per-machine.)

## Architectural Guidelines
*  **Lead's Rule:** Discuss class design (e.g., `GenericDriver` vs `WlanInterface` responsibilities) BEFORE execution. Treat the user as the Senior Lead.
