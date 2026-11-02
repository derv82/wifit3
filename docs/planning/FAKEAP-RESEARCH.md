# EvilTwin in other tools

How six tool families implement EvilTwin, read at the source level: Fluxion, airgeddon,
wifiphisher, aircrack-ng (airbase-ng + aireplay-ng), wifite2 (derv82 + kimocoder),
hostapd-mana / eaphammer, WiFi Pineapple (PineAP, KARMA/Jasager).

## Summary

| Tool | Model | Twin BSSID | Twin channel | Eviction | Twin security | What it captures |
|---|---|---|---|---|---|---|
| Fluxion | push | target, last nibble +1 | target's | continuous deauth (`aireplay -0 0` / mdk4) | open + portal | typed PSK, checked against a real handshake |
| airgeddon | push | target, one nibble flipped; SSID + zero-width space | target's | continuous (mdk4 amok / `--deauth 0` / auth-DoS) | open + portal | typed PSK, checked against a real handshake |
| wifiphisher | push + KARMA | random, OUI `00:00:00` | target's | continuous deauth + disassoc | open + portal | typed creds (optional cowpatty PSK check) |
| aircrack-ng | primitives | own MAC (`-a` to clone) | any (`-c`) | separate tool: `aireplay -0` | open, or WPA tags | EAPOL with `-F` |
| wifite2 (kimocoder) | push | own interface MAC | target's | deauth (aireplay) | open + portal | typed PSK, checked by wpa_supplicant |
| hostapd-mana / eaphammer | pull | own / placeholder | own (`-c`) | none; suppresses disconnects | WPA2 / EAP | M2 half-handshake (`WPA*02*`), EAP creds |
| WiFi Pineapple | pull | own / random | own, single | none (separate deauth module) | open (Evil WPA optional) | portal MITM; Evil WPA handshake / PMKID |

- **Push:** clone the SSID, deauth the client off the real AP, funnel it to the twin.
- **Pull:** answer the client's own probe requests and let it join voluntarily. Lure, not punt.

## Across all tools

- **CSA:** none use it to evict. Zero hits in Fluxion, wifiphisher, airgeddon and aircrack-ng.
  hostapd's `chan_switch` (`hostapd_cli.c:1303`, `ctrl_iface.c:2440`) is only a self-move
  (DFS, retune); mana and eaphammer never call it.
- **Channel:** push tools put the twin on the target's channel; pull tools sit on their own.
  None uses a separate decoy channel.
- **BSSID:** none use the target's exact BSSID. The off-by-one nibble keeps two identical
  beacons from colliding on one channel.
- **Twin security:** the push tools run an open twin + captive portal. The user taps the
  open network and types the password into a page; no auto-join to a WPA2 twin is needed.
- **Capture:** either (a) a typed PSK checked against a real handshake (aircrack/cowpatty),
  or (b) the client's M2 logged for an offline crack (mana, eaphammer, Evil WPA).
- **Deauth cadence:** push tools deauth continuously until stopped. aireplay baseline
  (`aireplay-ng.c` `do_attack_deauth`): 128 frames per round, 2 ms apart, both directions,
  reason 7. wifite2 (derv82) is the exception: `-0 1` per target every 15 s.
- **Modern clients:** the KARMA lineage (`hostapd.conf:9-16` mana_loud comment, PineAP
  docs) documents that iOS/Android stopped broadcast probing and randomize MACs, breaking
  classic KARMA. iOS is the most resistant.

## Per tool

### Fluxion
- Two stages: Handshake Snooper captures a real 4-way (`attacks/Handshake Snooper/attack.sh`),
  then Captive Portal phishes the PSK and checks it against that capture.
- BSSID: last nibble +1 (`fluxion.sh:1905-1906`), not configurable.
- Channel: target's (`attack.sh:685`); follows the real AP if it moves.
- Eviction: `aireplay-ng -0 0 -a <target>` or looping `mdk4 ... d -b <blacklist>`
  (`attack.sh:1437-1458`). Snooper: 100-frame burst every ~10 s (`Handshake Snooper/attack.sh:225`).
- Twin: open; dhcpd + dnsspoof + lighttpd + iptables DNAT. Optional airbase-ng backend with `-P`.
- Capture: typed key checked with cowpatty / aircrack-ng (`captive_portal_authenticator.sh`).

### airgeddon
- Five variants (`evil_twin_attacks_menu()`, `airgeddon.sh:8017`): AP-only, ettercap,
  bettercap sslstrip2, +BeEF, captive portal. WPA3 downgrade uses hostapd-mana for a WPA2
  twin (default passphrase "airgeddon").
- BSSID: `generate_fake_bssid()` flips one nibble (`:11776`); `generate_fake_essid()` adds a
  zero-width space (`:11794`).
- Channel: target's (`set_hostapd_config()`, `:11643`). "DoS pursuit mode" follows the real
  AP if it changes channel (needs a second interface).
- Eviction (`et_dos_menu()`, `:16842`): mdk4 amok (default), `aireplay --deauth 0`, or mdk
  auth-DoS. All continuous.
- Twin: open (`wpa=0`) + dhcpd + captive portal.
- Capture: `check.htm` runs `aircrack-ng -a 2 -b <bssid> -w <typed> <handshake>`, looks for
  `KEY FOUND!` (`:13595`). Needs a handshake first.

### wifiphisher
- Extensions on a second monitor interface (`common/extensions.py`): deauth (default),
  knownbeacons, lure10, handshakeverify, roguehostapdinfo.
- BSSID: random, OUI `00:00:00` (`interfaces.py:932`). SSID cloned.
- Channel: target's (`pywifiphisher.py:599`); the deauth extension hops, 2.4 GHz only (`deauth.py:164`).
- Eviction: disassoc then deauth, broadcast AP->client plus per-client both directions
  (`deauth.py:54-81,257-266`), no rate limit.
- Twin: roguehostapd, open + portal, `karma_enable=1` by default (`accesspoint.py:99`).
  Known Beacons cycles 60 SSIDs every 20 s. Lure10 spoofs Windows Location Service.
- Capture: phishing server (`phishinghttp.py:CaptivePortalHandler`); `-hC` checks a PSK with cowpatty.

### aircrack-ng
- airbase-ng lures, it doesn't evict. Manpage: "should encourage clients to associate with
  the fake AP, not prevent them from accessing the real AP." Beacons, answers auth/assoc,
  captures EAPOL with `-F`.
- BSSID: own MAC, `-a` to clone. Channel: `-c`. `-P` answers all probes. WPA tags: `-z` (WPA1), `-Z` (RSN).
- aireplay-ng deauth (`aireplay-ng.c:109`, `do_attack_deauth`): `-0 N` rounds, `-0 0`
  infinite. Directed: 64 to the client + 64 to the AP per round, 2 ms apart. Broadcast: 128
  per round. Reason 7.

### wifite2
- derv82: no EvilTwin (`# TODO: EvilTwin attack` in `attack/all.py`). Deauth for handshakes:
  `-0 1` per target every `wpa_deauth_timeout=15s`, broadcast then each client. A
  `ContinuousDeauth` thread exists (5 s interval, 5 bursts).
- kimocoder: `EvilTwin` class. hostapd on the target's channel with the interface MAC,
  deauth only, captive portal checked by wpa_supplicant. No EAPOL forgery.

### hostapd-mana / eaphammer
- Lure, not punt. `hostapd.conf:2-6`: "If you want a 'standard AP' that only looks like one
  network, don't enable [MANA]." KARMA answers directed probes per station; `mana_loud=1`
  re-broadcasts every SSID it hears to everyone.
- No deauth. Suppresses disconnects to keep a lured client
  (`mana_wpa_should_suppress_disconnect()`, `wpa.c:114`).
- BSSID: own by default; eaphammer ships `bssid=00:11:22:33:44:00`, `--bssid` to override.
  Channel: eaphammer default 1, `--channel`.
- Capture: M2 half-handshake as `WPA*02*...` (`mana/wpa.c:27`); eaphammer
  `capture_wpa_handshakes=1`.
- Enterprise: WPE MSCHAPv2 challenge/response, GTC downgrade for plaintext creds
  (`eap_user_methods.ini`), EAP-Success on bad creds, sycophant inner-EAP relay.

### WiFi Pineapple (PineAP, KARMA, Jasager)
- Probe response (Beacon Response + SSID Pool). Own or random BSSID, own single channel;
  the client finds it by scanning. Deauth is a separate optional module.
- Default twin is open (MITM / portal). Evil WPA module: clones the SSID only, captures a
  full handshake or PMKID.

## Observed behavior: wifit3 field tests (2026-10-17/18)

Sample: 1 router (GL-MT3000, 2.4 GHz, target on CH11), 1 client each of iOS, macOS and
Windows, 1 run per row unless stated. Passive MT7925AU sniffer on the twin's channel.
Linux and Android are untested: the Android phone (OnePlus 6T) can't join this router's
WPA3 (no H2E).

Twin configurations:
- **Same-channel:** twin on the target's channel, BSSID = target BSSID + 1.
- **Decoy-channel:** twin on another channel (CH1), same BSSID as the target, CSA beacons
  point clients at it.

| Security | PMF | Setup | Client | Result |
|---|---|---|---|---|
| WPA2 | Disabled | AutoDeauth, deauth, no twin | not recorded | Clients reconnect to the real AP; handshake captured there. BTM evicts too. |
| WPA2 | Disabled | Same-channel twin, BTM every 5 s, no deauth | iOS | Reassociated to the twin with the password saved for the target; M2 captured. |
| WPA2 | Required | AutoDeauth, CSA only, no twin | iOS, macOS, Windows | Each reconnected to the real AP; handshake captured (1 run each). |
| WPA3->2 transition | Optional | Decoy-channel twin + CSA | iOS | Unprompted: one probe, nothing after the probe response. User taps the twin and types the password: M2 captured. |
| WPA3->2 transition | Optional | Decoy-channel twin + CSA | Windows | Follows the CSA to CH1, probes the SSID, then sends QoS/null data to the twin as if still associated. Never authenticates. Deauth (reason 7) in reply to that data: 1 of 2 ACKed, 1 deauth sent back, no reconnect to the twin. |
| WPA3->2 transition | Optional | Decoy-channel twin + CSA | macOS | Never joined the twin. |

Notes:
- WPA3->2 Optional: SAE clients ignore deauth; CSA is what moves iOS and Windows.
- WPA2 Required: an earlier run with a twin up had iOS refusing to auto-join. With no twin,
  CSA alone got it to rejoin the real AP.
- A first same-channel BTM run was invalid: iOS still held a password saved from an earlier
  twin. Forget the twin network on the client between runs.
- The sniffer only hears what reaches it, and data payloads are encrypted with the real AP's
  keys, so it can't say what the Windows data frames carry.

Untested: WPA2 PMF Optional (any setup); WPA2 with a decoy-channel twin; WPA3->2 PMF
Required; same-channel twin on anything but WPA2 PMF Disabled; Linux and Android clients.
WPA3-only (PMF Required) is out of scope: no campaigns run against it.

Inferred, not measured: a same-channel twin has no lever against PMF clients, because the
real AP answers the client's SA Query. Unconfirmed hunch: the twin's RSN IE differing from
the target's is what stops clients from starting auth.
