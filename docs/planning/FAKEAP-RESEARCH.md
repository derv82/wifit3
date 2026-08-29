*Prompt: "How do major popular tools implement EvilTwin? Same/diff channel? same/diff BSSID? De-auth method?*

# EvilTwin in the wild: how six major tools implement it

Purpose: ground wifit3's EvilTwin design in how established open-source tools actually
do it. Six tool families were read at the source level (bash, C, Python, and vendor
docs), each answering the same eight questions. This records what they do and where
wifit3 diverges, so the campaign rewrite (see `EVILTWIN.md`) rests on field practice
instead of assumptions.

Sources read: Fluxion, airgeddon, wifiphisher, aircrack-ng (airbase-ng + aireplay-ng),
wifite2 (derv82 + kimocoder forks), hostapd / hostapd-mana / eaphammer, and Hak5 WiFi
Pineapple (PineAP) with its KARMA/Jasager ancestry.

## The one-line answer

wifit3's *capture* mechanic (forge M1, grab the victim's M2, crack offline) is
field-standard: it is exactly what hostapd-mana does. wifit3's *eviction* mechanic
(Deauth + offensive CSA beacons + a decoy channel) is used by none of them. Zero of the
six weaponize CSA. None sit the twin on a decoy channel. None reuse the exact target
BSSID. That divergence is where the doubts are well-founded.

## Comparison matrix

| Tool | Philosophy | Twin BSSID | Twin channel | Punt method | Twin encryption | Credential/handshake path |
|---|---|---|---|---|---|---|
| Fluxion | push (clone+deauth) | off-by-one nibble | same as target | continuous deauth (`aireplay -0 0` / mdk4 amok) | open + portal | typed PSK, validated offline vs a real handshake |
| airgeddon | push | off-by-one + zero-width-space SSID | same | continuous (mdk4 amok / `--deauth 0` / auth-DoS) | open + portal (WPA2 only in WPA3-downgrade via mana) | typed PSK validated vs handshake |
| wifiphisher | push + KARMA | random own (`00:00:00` OUI) | same | continuous deauth+disassoc | open + portal + KARMA/Known-Beacons | typed creds (optional cowpatty PSK check) |
| aircrack-ng | lure primitive | own MAC (`-a` to clone) | any (`-c`) | `aireplay -0` (separate tool): 128 frames/round, 2ms, both dirs | open (`-P` KARMA) or WPA tags | airodump passive + external deauth |
| wifite2 (kimocoder) | push | own iface MAC | same | deauth-only (aireplay) | open + hostapd portal | typed PSK validated by wpa_supplicant |
| hostapd-mana / eaphammer | pull (KARMA/MANA) | own/placeholder (`--bssid` opt) | own (`-c` opt) | none (mana suppresses disconnect) | WPA2 / EAP | EAPOL M2 half-handshake (`WPA*02*`) + EAP creds/GTC downgrade |
| WiFi Pineapple / PineAP | pull (KARMA) | own/random | own single channel | none (deauth = separate module) | open (Evil WPA opt) | portal MITM; Evil WPA full/PMKID handshake |
| **wifit3 today** | **push** | **same or distinct** | **DECOY (different)** | **deauth + CSA beacons -> decoy** | **WPA2 (WPA3 downgrade)** | **forge M1 -> capture victim M2** |

## Two schools

**Push (clone + deauth):** Fluxion, airgeddon, wifiphisher, wifite2 (kimocoder). Clone
the SSID, stand up a twin, deauth the client off the real AP, funnel it to the twin.

**Pull (KARMA / probe-response):** hostapd-mana, eaphammer, WiFi Pineapple/PineAP. Do
not clone one AP and do not deauth. Answer the client's own probe requests ("yes, I am
that SSID") and let it associate voluntarily. hostapd-mana even *suppresses* disconnects
(`mana/wpa.c:114`) to keep a lured client attached.

wifit3 is a push tool, but a variant nobody else runs.

## Seven findings that bear on wifit3

1. **CSA: nobody uses it to punt.** Grep-verified zero in Fluxion, wifiphisher,
   airgeddon; absent from the aircrack-ng suite; and present in hostapd only as the
   legitimate `hostapd_cli chan_switch` self-move (DFS/retune), never wired into an
   evil-twin punt in mana or eaphammer. PineAP has no reason for it (probe-response).
   wifit3's offensive-CSA punt has no analog in any shipped tool.

2. **Channel: everybody is same-channel (push) or own-single-channel (pull).** No tool
   sits the twin on a decoy channel while punting from the target channel. Push tools
   put the twin on the *target's* channel; pull tools sit on their own channel and let
   the client's own scan find them. wifit3's decoy channel is unique.

3. **BSSID: nobody uses the exact target BSSID.** Fluxion (`fluxion.sh:1905`) and
   airgeddon (`generate_fake_bssid()`, `airgeddon.sh:11776`) bump a single nibble;
   wifiphisher / mana / PineAP use a random or own MAC. The off-by-one exists
   *specifically* so two byte-identical beacons never collide on one channel: exactly
   the "iPhone kills WiFi on conflicting beacons" failure observed in testing.

4. **Encryption: the three closest tools run an OPEN twin + captive portal, not WPA2.**
   Fluxion, airgeddon, wifiphisher, and wifite2(kimocoder) all stand up an *open* AP and
   a web portal. They never make the client auto-join a WPA2 twin. A deauthed, frustrated
   user manually taps the open network and types the password into a page. This sidesteps
   the "iOS refuses to silently downgrade a remembered WPA3/WPA2 network" wall recorded in
   `EVILTWIN.md`: it is why the portal tools win on iOS where wifit3 does not.

5. **Capture: two mechanisms.** (a) Portal-typed-PSK validated against a real handshake
   with aircrack/cowpatty (Fluxion `authenticator.php`, airgeddon `check.htm`,
   wifiphisher `-hC`, wifite2). (b) EAPOL M2 half-handshake logged for offline crack
   (hostapd-mana `mana/wpa.c:27`, eaphammer `capture_wpa_handshakes=1`, PineAP Evil WPA).
   wifit3 is squarely school (b), and mana does the identical thing (accept any client,
   log the M2 as a hashcat artifact). So wifit3's capture is validated; only the eviction
   diverges.

6. **Deauth cadence: continuous, not periodic.** Every push tool deauths open-endedly
   (`aireplay -0 0` / `--deauth 0` / mdk4 amok) until the operator stops it. The
   aireplay baseline (`aireplay-ng.c` `do_attack_deauth`): 128 frames per round, 2ms
   apart, deauthing *both* directions (client->AP and AP->client), reason code 7.
   wifite2 (derv82) is the lone periodic exception: `-0 1` per target every
   `wpa_deauth_timeout=15s`. wifit3's 30s bounded punt is milder than the field norm.

7. **iOS resistance is the shared enemy, and the portal design is the shared answer.**
   The KARMA lineage (`hostapd.conf:9-16` mana_loud comment; PineAP docs) explicitly
   documents that modern iOS/Android stopped broadcast-probing and randomize MACs, which
   broke classic KARMA and forced MANA-loud + Known-Beacons countermeasures. The push
   tools route around the same resistance a different way: open twin + portal needs no
   auto-join at all.

## Per-tool detail

### Fluxion (push, clone + portal)
- Two stages: Handshake Snooper captures a real 4-way handshake first
  (`attacks/Handshake Snooper/attack.sh`), then Captive Portal phishes the PSK and
  validates it offline against that `.cap`.
- BSSID: target MAC with last nibble +1 (`fluxion.sh:1905-1906`), not user-configurable.
- Channel: same as target (`attack.sh:685`, hostapd `channel=`); a tracker follows the
  real AP if it hops, but never a decoy channel.
- Punt: continuous broadcast deauth. Jammer runs `aireplay-ng -0 0 -a <target>` or
  looping `mdk4 ... d -b <blacklist>` (`attack.sh:1437-1458`). Snooper deauther is a
  100-burst loop every ~10s (`attacks/Handshake Snooper/attack.sh:225`).
- Twin: OPEN network (hostapd `wpa` absent), plus dhcpd + dnsspoof + lighttpd + iptables
  DNAT (`attack.sh`). Optional airbase-ng backend uses `-P` (answer all probes).
- Capture: typed key checked with `cowpatty`/`aircrack-ng` against the `.cap`
  (`captive_portal_authenticator.sh`); zero false-accepts.
- CSA: none.

### airgeddon (push, clone + portal; multiple variants)
- Five evil-twin variants (`evil_twin_attacks_menu()`, `airgeddon.sh:8017`): AP-only,
  ettercap sniff, bettercap sslstrip2, +BeEF, and captive portal. Separate WPA3-downgrade
  path uses `hostapd-mana` for a WPA2 twin (default passphrase "airgeddon").
- BSSID: `generate_fake_bssid()` flips one nibble (`:11776`); ESSID gets a zero-width
  space appended (`generate_fake_essid()`, `:11794`).
- Channel: same as target (`set_hostapd_config()`, `:11643`). "DoS pursuit mode" *chases*
  the real AP if it changes channel: the inverse of CSA, and it needs a second interface.
- Punt: three menu options (`et_dos_menu()`, `:16842`): mdk4 amok (default), `aireplay
  --deauth 0`, or mdk auth-DoS. All continuous, no rate cap, no bounded period.
- Twin: OPEN (`wpa=0`) + dhcpd (`192.169.1.0/24`) + captive portal.
- Capture: `check.htm` CGI runs `aircrack-ng -a 2 -b <bssid> -w <typed> <handshake>`
  and looks for `KEY FOUND!` (`:13595`). Requires a handshake captured first.
- CSA: none (grep-zero).

### wifiphisher (push + KARMA, portal-focused)
- Extensions architecture on a second monitor interface (`common/extensions.py`):
  deauth (default), knownbeacons, lure10, handshakeverify, roguehostapdinfo.
- BSSID: randomized, OUI `00:00:00` (`interfaces.py:932`); never the target's. ESSID is
  cloned.
- Channel: rogue AP on the target's channel (`pywifiphisher.py:599`); deauth extension
  hops (2.4GHz only, `deauth.py:164`).
- Punt: `_craft_packet` sends disassoc (subtype 10) then deauth (subtype 12), broadcast
  AP->client and bidirectional per-client (`deauth.py:54-81,257-266`); tight send loop,
  no rate limit.
- Twin: roguehostapd, OPEN + captive portal, `karma_enable=1` default-on
  (`accesspoint.py:99`). Known Beacons cycles 60 SSIDs every 20s (`knownbeacons.py`).
  Lure10 spoofs Windows Location Service.
- Capture: Tornado phishing server harvests typed creds
  (`phishinghttp.py:CaptivePortalHandler`). Optional `-hC` runs cowpatty to validate a
  PSK against a real handshake.
- CSA: none.

### aircrack-ng suite (primitives)
- airbase-ng is a lure, not a punt. Manpage: "should encourage clients to associate with
  the fake AP, not prevent them from accessing the real AP." Forges beacons, answers
  auth/assoc, captures EAPOL with `-F`. No deauth, no portal, no channel move.
- BSSID: interface's own MAC by default; `-a` to clone the target. Channel: `-c`, any.
- KARMA `-P`: answer all probe SSIDs. WPA tags via `-z` (WPA1) / `-Z` (WPA2/RSN).
- aireplay-ng deauth (attack 0, `aireplay-ng.c:109` template, `do_attack_deauth`):
  `-0 N` = N rounds, `-0 0` = infinite. Directed = 64 frames to client + 64 to AP per
  round, 2ms apart. Broadcast = 128 frames/round. Reason code 7.
- CSA: none in the suite (belongs to mdk4, not aircrack-ng).

### wifite2 (fork-dependent)
- derv82/wifite2: NO evil-twin (literal `# TODO: EvilTwin attack` stub in
  `attack/all.py`). Deauth cadence for handshake capture: `-0 1` per target every
  `wpa_deauth_timeout=15s`, broadcast first then each client (`attack/wpa.py`,
  `config.py`). A `ContinuousDeauth` thread also exists (5s interval, 5 bursts).
- kimocoder/wifite2: has an `EvilTwin` class, but it is hostapd on the *same* channel
  with the interface MAC, deauth-only, and a captive portal validated by wpa_supplicant.
  No EAPOL forgery, no M2 capture, no CSA.

### hostapd / hostapd-mana / eaphammer (pull, KARMA/MANA)
- Philosophy is lure, not punt. `hostapd.conf:2-6`: "If you want a 'standard AP' that
  only looks like one network, don't enable [MANA]." Classic KARMA answers a directed
  probe per-station; `mana_loud=1` re-broadcasts every observed SSID to everyone.
- No deauth in either workflow. mana actively suppresses disconnects
  (`mana_wpa_should_suppress_disconnect()`, `wpa.c:114`) to keep the lured client on.
- BSSID: not set by default (own MAC). eaphammer ships placeholder
  `bssid=00:11:22:33:44:00`, `--bssid` to override. Channel: eaphammer default 1,
  `--channel` to match target. No decoy channel, no same-vs-distinct branching.
- Capture: WPA2 M2 half-handshake logged as `WPA*02*...` hashcat form (`mana/wpa.c:27`);
  eaphammer `capture_wpa_handshakes=1`. Enterprise: WPE MSCHAPv2 challenge/response,
  GTC downgrade for plaintext creds (`eap_user_methods.ini`), EAP-Success-on-bad-creds,
  and sycophant live inner-EAP relay.
- CSA: hostapd's `chan_switch` (`hostapd_cli.c:1303`, `ctrl_iface.c:2440`) is a
  legitimate DFS/retune self-move only; mana/eaphammer never call it to punt.

### WiFi Pineapple / PineAP / KARMA / Jasager (pull)
- Fundamentally probe-response (Beacon Response + SSID Pool). Presents its own or a
  randomized BSSID (never the real AP's), on its own single channel; the client comes to
  it via its own scan/roam. Deauth is a separate optional module, not part of PineAP.
- Default is an OPEN network for MITM/portal. WPA2-PSK is a bolt-on (Evil WPA module):
  clones only the SSID, captures a full or PMKID-partial handshake for offline cracking.
- Documents why KARMA decayed on modern devices (no broadcast probing, MAC
  randomization) and the MANA-loud / Known-Beacons countermeasures. iOS most resistant.
- CSA: not used; there is nothing to switch, because the victim was never pried off a
  real AP.

## Implications for wifit3

The research splits wifit3's design into a sound half and a doubtful half.

**Sound (keep):** the WPA2 EAPOL capture. Accepting any associating client and logging
its M2 for an offline crack is exactly hostapd-mana's mechanic. No change needed.

**Doubtful (the divergence):** eviction. Deauth + offensive CSA + decoy channel is a
combination no shipped tool uses. Specifically:
- CSA to punt: no precedent; testing did not show it hard-kicking the iPhone. Candidate
  for removal or demotion to an experimental knob.
- Decoy channel: no precedent. Everyone is same-channel (push) or own-channel (pull).
- Same-BSSID on the same channel: the exact beacon-collision that made the iPhone kill
  WiFi. The field's off-by-one BSSID is the established fix.
- 30s bounded punt: milder than the field's continuous deauth.

**Two strategic directions** (a lead decision, not yet made):

- **A. Keep WPA2-EAPOL capture, adopt the field's eviction.** Drop CSA and the decoy
  channel. Put the WPA2 twin on the *same* channel with an off-by-one BSSID; deauth
  continuously, both directions, ~2ms spacing. This is the hostapd-mana/eaphammer model.
  Still depends on the client auto-associating to a WPA2 twin, which iOS resists, so it
  may keep capturing on Android/older clients and keep missing iOS.

- **B. Adopt the proven clone-and-portal design.** Open twin + captive portal on the
  same channel with an off-by-one BSSID, continuous deauth, and validate the typed PSK
  against a handshake wifit3 already captures. This is the Fluxion/airgeddon architecture
  and the only one shown to work against iOS in the field, because it needs no auto-join.
  Larger build (portal + DHCP + DNS + web server), but it reuses wifit3's existing
  handshake capture as the validator.

**Modal knobs, reconsidered against the findings:** the "EvilTwin Channel" dropdown and
the CSA punt checkbox both encode the two mechanics with no field precedent. If direction
A or B is taken, channel collapses to "same as target" and CSA drops (or becomes an
experimental toggle). Deauth should default to continuous rather than a fixed cycle; the
30s value stays useful only as a wifit3-measured capture-rate optimum for the current
design, not as a field norm. The BSSID control is better as a principled off-by-one than
a full randomize, so the twin looks like the target while never colliding.
