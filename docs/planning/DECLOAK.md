# Decloaking hidden APs

Status: the Focus campaign is implemented and confirmed against real APs, including one
neighbouring AP recovered through the association sweep. The candidate modal and
persistence are not built.

## Goal

Recover a hidden AP's SSID so the SSID-dependent attacks (EvilTwin, PMKID, WPS, WEP
fake-auth) can run against it. `AccessPoint.is_hidden` is the predicate; a success sets
`ap.ssid` + `ap.decloak_method` and clears those disabled buttons.

## What already exists

`campaigns/decloak.py` — `DecloakCampaign`, a Focus campaign on `h`, visible only when
`ap.is_hidden` and refused when the AP has no siblings. Two mechanisms, in order:

1. **A directed Probe Request per candidate.** An AP answers the probe that names it; the
   reply runs through the parser and `WlanSink.decloak` sets `ap.ssid`.
2. **An Association Request per candidate**, once every probe has gone unanswered. An AP
   accepts the Assoc Req naming it and ignores the rest.

Helpers:

- `candidates_from_sibling(sibling_ssid)` appends every `SIBLING_SUFFIXES` entry
  (`-Guest`, `_5G`, `-IoT`, …), dropping duplicates and names over 32 octets. This list is
  in-house; keep growing it, do not import anyone else's.
- `named_sibling_ssid(array, hidden)` picks the base: whichever of `hidden.siblings` has an
  SSID and the most beacons.
- `candidates=` replaces the generated list. This is the hook the modal drives.

## What the association sweep depends on

Measured with `scripts/decloak/assoc_oracle_lab.py` against a consumer guest network, on
both an RTL8188EUS and an RTL8822BU.

- **A wrong SSID draws silence, not a reject status.** Auth returns status 0 whatever SSID
  we claim, because open-system auth carries no SSID; only the Assoc Resp discriminates.
  Silence is the refusal. A sweep that waits for an explicit reject code calls every AP of
  this kind inconclusive and gives up.
- **Active monitor is mandatory.** Without `set_fake_mac` the AP's Auth Resp goes unACKed,
  authentication never completes, and each Assoc Req comes back as a class-2 deauth
  (reason 6) that the AP retransmits to its own retry limit. See `docs/ACKS.md`.
- **One Auth Req covers the whole sweep.** A refused association leaves the station
  `AssocState.AUTHENTICATED`, and a disassoc returns it there; only a deauth drops it to
  `UNAUTHENTICATED`. Re-authenticating per candidate makes the AP tear down the session we
  just built.
- **An invented control SSID goes first.** An AP that accepts a random name accepts
  anything, and the sweep can prove nothing against it.
- **A PMF-required AP never authenticates us.** The sweep bails after the first Auth Req
  rather than working through the list.

## Persistence

Store the learned `BSSID -> SSID` in the existing `config.toml`, in a `[decloak]` table
with quoted keys, beside `silenced_bssids`. No separate file, no second format.

Do not assign a stored SSID on sight. BSSIDs get reassigned and guest SSIDs get renamed, so
assigning one blindly displays a stale name as confirmed and records a `decloak_method`
nobody observed. Promote the stored name to the front of the candidate list instead
(`candidates=[stored, *candidates_from_sibling(...)]`) and let the normal path confirm it in
a single probe. Log `Decloaking {bssid} ("{stored_ssid}")...`.

That leaves a remembered name resolving only once the user starts the campaign. A
Preferences toggle, "re-decloak hidden networks on load", would run the one-candidate
confirm automatically as known hidden BSSIDs reappear.

## The candidate modal

Not built, and it is the piece that reaches a hidden AP with no visible sibling — which is
most standalone hidden APs. Trigger on a selected hidden AP, from Scanner and Focus:

- A big scrollable textarea, one candidate SSID per line, pre-filled from
  `candidates_from_sibling`.
- Load a wordlist file into the textarea (MDK4-style SSID list).
- Templating: `$ssid` is a base (a visible sibling from `AccessPoint.siblings`, else typed).
  Lines like `$ssid Guest`, `$ssid 5g`, `$ssid IoT` expand for a directed decloak.
- `[Decloak]` / `[Cancel]`. On Decloak: split lines, expand `$ssid`, pass as `candidates`.

The user must see exactly which SSIDs will be sent, and be able to edit the list. Rate-limit
the sweep so a long wordlist cannot act as a DoS against the router.

Open question: a "Defaults" button that pre-loads common router-default SSIDs. Shape TBD.

## Future ideas

### CSA-triggered decloak

Spoof a CSA/ECSA beacon as a hidden AP (`dot11/csa.py` `build_csa_beacon`) to move its
clients off-channel, then read the SSID from a returning client's directed Probe Request or
Auth+Assoc on the destination channel. CSA carries no SSID, so it works while the AP is
still hidden. Needs two interfaces, one sending on the AP's channel and one listening on the
destination channel.

Not implemented, and invasive: it disconnects real clients, which is a DoS.

One flaw to record before anyone builds it. An earlier sketch here proposed standing up a
minimal AP on the destination channel so a returning client gets as far as Assoc. That
cannot work for a hidden AP: the decoy has no SSID to advertise, so it stays silent on a
directed probe naming the real SSID, and answers a wildcard probe with an empty SSID. The
client never associates. To be convincing the decoy would have to advertise the very name
being searched for.

### Other SSID sources

802.11 elements that name a BSS other than the one transmitting: Multiple BSSID (71),
Reduced Neighbor Report (201), OWE Transition, and FILS Discovery action frames. All
receive-only, no injection. Not implemented, and not verifiable without routers configured
to emit them.
