# Wifit3 — Features & QoL Backlog

Known bugs live in `BUGS.md`.

---

### Client fingerprinting

**Problem.** Clients show bare MACs; a device class (phone / laptop / PS5 / IoT) speeds target
selection. IoT (Ring/Nest/Roku/FireTV) is highest-value for scoping.

**Approach.** Emoji left of the BSSID, one `fingerprint.py`, no DB: ~50 hardcoded OUI prefixes
+ IE fingerprinting for ambiguous OUIs (Murata/Intel modules); returns `(emoji, class,
confidence)`, blank if low; full breakdown in the Focus detail panel.

**Complexity.** Moderate: display is the hard part, not the resolver. (Killed a full
OUI→vendor DB in the Scanner table: cells too cramped for vendor strings, and an OUI names the
Wi-Fi *module* maker, not the device: disambiguation needs IE fingerprinting anyway.)

### About page / Check-for-updates

If the user has internet connection, it's trivial to query
[the releases page](https://github.com/derv82/wifit3/releases) to fectch the latest version,
compare with the current version, and show a Toast notification about the newest version,
clicking Toast notification -> opens releases page.

We could automate this as well (opt-**in**), in Preferences: `[x] Automatically check for updates`

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

### EAP-MSCHAPv2 / PEAP via Evil Twin

Most enterprise Wi-Fi is PEAP-MSCHAPv2, which cracks with hashcat `-m 5500` (DES half near-
instant via crack.sh): recovering the *domain* credential is a far higher value than a PSK,
PEAP wraps MSCHAPv2 in TLS, so it **can't be captured passively**. Stand up an Evil Twin 
so the client auths to *you*.

Some things we'll need:
- target-ESSID beacons,
- RADIUS/EAP state machine
- cert handling.

When a second hashcat mode lands (`-m 4800`/`5500`), the save layer needs a per-attack
(mode + line-format) map instead of the hardcoded `-m 22000`.

------------

## Deferred / Chopping Block

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
