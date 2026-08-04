# Wifit3 — Features & QoL Backlog

Known bugs live in `BUGS.md`.

---

## Low Priority

### Test & Fix macOS support

Figure out how to detect & access drivers from userland in OSX.

The viable path is a **codeless kext** (Info.plist only) per supported card.
Each plist declares the adapter's VID:PID with a high `IOProbeScore` so the kernel
binds the do-nothing kext and leaves the USB interface unclaimed for libusb. 
Unverified. No macOS hardware tested. Parked until someone wants it.

### Client fingerprinting

**Problem.** Clients show bare MACs; a device class (phone / laptop / PS5 / IoT) speeds target
selection. IoT (Ring/Nest/Roku/FireTV) is highest-value for scoping.

**Approach.** Emoji left of the BSSID, one `fingerprint.py`, no DB: ~50 hardcoded OUI prefixes
+ IE fingerprinting for ambiguous OUIs (Murata/Intel modules); returns `(emoji, class,
confidence)`, blank if low; full breakdown in the Focus detail panel.

**Complexity.** Moderate: display is the hard part, not the resolver. (Killed a full
OUI→vendor DB in the Scanner table: cells too cramped for vendor strings, and an OUI names the
Wi-Fi *module* maker, not the device: disambiguation needs IE fingerprinting anyway.)

### VAULT — loot manager ("HACKLEBOX")

**Problem.** Half of Wifite's UX is effectively the OS file manager: squinting at `captures/` full
of long BSSID-encoded filenames. The loot (handshakes, PMKIDs, cracked PSKs) deserves a real view,
not a directory listing.

**What.** One screen that owns everything we've captured/cracked: handshakes, PMKIDs, PSKs,
passwords, the occasional WPS PIN (→ its PSK), WEP keys (nobody uses WEP, but still). Per-entry:
add / remove / export / copy. Bulk: **Export all as Zip**, **Show directory** (`open captures/` /
`explorer.exe captures/`) for the folks who still want the files.

**Check button.** Re-authenticate against the live AP and confirm a stored PSK still works. The
association layer we're untangling now is exactly the primitive this needs (open-auth + assoc +
4-way with the candidate PSK). Rare to *have* a plaintext password, but when we do, verifying it is
a genuinely nice touch.

**Launch Hashcat.** Per-entry button to fire hashcat with the right mode/hashline (leans on the
per-attack mode map noted in the enterprise graveyard entry). Cracked PSKs auto-add back into the
VAULT. The loop closes itself.

**Complexity.** Moderate: mostly a new screen over the existing `persist/save` + `crack/hc22000_format` layers;
the "Check" path reuses the association primitive; hashcat launch is a subprocess + parse.

------------

## Chopping Block / Graveyard

### WPS improvements - Low priority (who even has a vulnerable WPS router?)

The WPS engine is built, offline-proven, and HW-validated (full PIN crack on AirLink). Gaps:
- **Lock-cycle matrix** — only AirLink soft-lock tested; exercise no-lock, long cooldowns, hard-lock.
- **Terminal hard-lock escape** — `lock.py` learns a measured backoff but loops forever on a
  perma-locked AP; bail after N zero-progress cycles and tell the user.
- **Focus WPS panel** (passive-by-default, behind a button).
- **PixieWPS** — designed in `campaigns/wps/README.md` (native, all 5 modes, no binary).
  Deferred on effort + one real dep call: **numpy**, wanted to keep the Realtek RTL819x/eCos
  2³¹–2³² seed sweep interactive (Ralink/MediaTek instant). The old glibc-dep worry is a
  non-issue (`random()` is ~30 reimplementable lines). Tractable, not a wall.

---

## Rogue AP Graveyard

**Problems.**
1. EvilTwin/RogueAP requires responses within microsecond for ACKs.
  - We cannot achieve this from software <-> USB (multi-millisecond latency).
  - Hard-MACs that auto-ACK *could* be considered. We don't want card-specific solutions!
2. No native AP/STA support on most cards.
  - We skipped most/all of the STA/AP modes from the wireless drivers we ported.
  - Monitor + Inject was the goal.
  - Rewriting all drivers to support STA/AP = Significant effort.

### EAP-MSCHAPv2 / PEAP via Rogue AP / Evil Twin — "active", big build

Most enterprise Wi-Fi is PEAP-MSCHAPv2, which cracks with hashcat `-m 5500` (DES half near-
instant via crack.sh): recovering the *domain* credential, far higher value than a PSK. The
marquee enterprise capability. PEAP wraps MSCHAPv2 in TLS, so it **can't be captured
passively**. Stand up a rogue AP / evil twin so the client auths to *you*. Active, TX-heavy,
AP-impersonating → behind the explicit-action gate; large build (target-ESSID beacon, RADIUS/
EAP state machine, cert handling). Our `campaigns.campaign` format could compose it cleanly.
worth a design pass, and an area to beat Wifite2 (no native enterprise).

When a second hashcat mode lands (`-m 4800`/`5500`), the save layer needs a per-attack
(mode + line-format) map instead of the hardcoded `-m 22000`.

## WPA3 downgrade upgrade: EvilTwin

The Focus **WPA Downgrade** button reads as dead because the implemented path is weak. Both
paths win the same prize: the client's **EAPOL M1+M2** for a *WPA2* assoc (M2's MIC is all an
offline PSK crack needs), and both work **only on WPA3-transition** APs; Transition-Disable
kills them.

- **Path 1: passive (implemented, weak).** Forge WPA2-only beacons/probe-resps so a client
  downgrades and 4-ways with the *real* AP, sniffed passively. But the real AP still advertises
  SAE on-channel, so a sane client picks SAE → nothing to capture. (Never confirmed to inject on
  HW.) `campaigns/wpa3_downgrade.py`.
- **Path 2: evil twin (the reliable build).** Rogue AP (same SSID/BSSID, ideally a different
  channel), WPA2-only; accept auth+assoc, **send M1 yourself** (random ANonce), capture M2.
  Deterministic. A minimal AP responder in the inject path (beacon/probe/auth/assoc/M1), *not*
  a hostapd shell-out (Linux-only, breaks cross-platform). Feature-scale.

**Near-term QoL:** disable/annotate the button unless the target is WPA3-transition, and log
"passive: waiting for a natural reconnect (minutes–hours)" so it stops looking broken.
