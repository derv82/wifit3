# Decloaking hidden APs

Status: spec / design. The `d` modal, the Focus campaign, and the CSA path below are TODO.

## Goal

Recover a hidden AP's SSID so the SSID-dependent attacks (EvilTwin, PMKID, WPS, WEP
fake-auth) can run against it. `AccessPoint.is_hidden` is the predicate; a success sets
`ap.ssid` + `ap.decloak_method` and clears those disabled buttons.

## Persistence

We persist state across runs, so remember decloaked APs: store the learned
`BSSID -> SSID`. When that BSSID reappears still-hidden on a later run, don't trust the
stored name blindly. Send one directed probe-req for it (`candidates_override=[stored_ssid]`)
and let the normal decloak path confirm it. Log `Decloaking {bssid} ("{stored_ssid}")...`.

## What already exists

`campaigns/decloak.py` — `DecloakAttack`: active decloak by directed probe-req. For each
candidate SSID it injects a directed probe; a hidden AP answers the one carrying its real
name, and that probe-resp reveals the SSID.

- `build_candidates(base)` expands a visible sibling's SSID over `SIBLING_SUFFIXES`
  (`-Guest`, `_5G`, `-IoT`, …). This list is in-house; keep growing it, do not import
  anyone else's.
- `candidates_override`: feed an explicit SSID list, bypassing `build_candidates`. This is
  the hook the modal drives.

## Decloak campaign (Focus)

Wrap `DecloakAttack` as a Focus campaign for one AP, in the same button-row shape as the
others (`visible()` / `ineligible_reason()`). Visible only when `ap.is_hidden`.

## The `d` modal

Trigger `d` on a selected hidden AP. Scanner had this before; restore it and add it to
Focus too. It opens:

- A big scrollable textarea, one candidate SSID per line, pre-filled from
  `build_candidates`.
- Load a wordlist file into the textarea (MDK4-style SSID list).
- Templating: `$ssid` is a base (a visible sibling from `AccessPoint.siblings`, else typed).
  Lines like `$ssid Guest`, `$ssid 5g`, `$ssid IoT` expand for a directed decloak.
- `[Decloak]` / `[Cancel]`. On Decloak: split lines, expand `$ssid`, feed as
  `candidates_override` to the campaign.

Open question: a "Defaults" button that pre-loads common router-default SSIDs. Shape TBD.

## CSA-triggered decloak (idea, EvilTwin-shaped)

A one-shot "Decloak all hidden" that needs no wordlist: spoof a CSA/ECSA beacon as each
hidden AP (`dot11/csa.py` `build_csa_beacon`; the eviltwin punter already does this) to
move its clients off-channel. On the destination channel a returning client reveals the
SSID by name, in either a directed Probe Request or an Auth+Assoc. CSA carries no SSID, so
it works while the AP is still hidden.

This is a two-interface attack, like EvilTwin's `twin_iface` + `punt_iface`, and the more
complex path of the two:

- One interface sends the CSA on the AP's real channel; another listens on the destination
  channel for the returning client. One device can't reliably do both.
- A client may stop at a directed Probe Request (already enough) or push on to Auth+Assoc.
  To guarantee it reaches Assoc we likely have to answer auth/assoc on the destination
  channel, i.e. stand up a minimal AP there. That is the EvilTwin overlap.
- Considerations: CSA beacon count before the switch; how long clients take to return;
  whether a given client honours CSA at all; and choosing a destination channel we can both
  send the CSA toward and listen on.

The wordlist / directed-probe modal above is the simple path and should land first.

## Known gap

`DecloakAttack` registers a forged source MAC but never unregisters it / clears the fake
MAC on exit, so the forged STA leaks past the run. Fold into campaign-lifecycle teardown.
