# WPS attacks

WSC PIN brute-force and PBC (push-button) PSK capture, native Python on top of
`WlanInterface`. WPS is EAP-over-EAPOL (ethertype `0x888e`) inside ordinary 802.11 data
frames, the same inject+sniff path PMKID uses. EAP-Expanded/WSC is parsed inside this
module via the RX callback rather than in the hot-path beacon parser.

## Architecture

The WSC spec primitives are pure and live in `dot11/wsc/`, reused by both roles:
- `dot11/wsc/crypto.py`: DH over RFC-3526 MODP group 5 (`pow()`), HMAC-SHA256, the WPS
  KDF, DHKey/KDK/AuthKey/KeyWrapKey derivation, AES-128-CBC (M5/M7 encrypted settings),
  PIN checksum, and `check_pin_half`. Pure functions with no I/O, offline-testable
  against hostapd/pixiewps vectors.
- `dot11/wsc/messages.py`: the WSC TLV codec + EAP/EAPOL framing (Expanded type 254, WFA
  vendor ID). Builds M2/M4/M6; parses M1/M3/M5/M7/NACK/ACK/DONE.
- `dot11/wsc/assoc_ie.py`: the WPS Association vendor IE (registrar vs enrollee intent).

The attack machinery lives here in `campaigns/wps/`:
- `registrar.py` (`WpsRegistrar`): the per-PIN EAP/WSC state machine. Inject callback in,
  RX queue out; it knows nothing about USB. Returns FIRST_HALF_WRONG / SECOND_HALF_WRONG /
  SUCCESS / TIMEOUT / PROTO_ERROR, owns the session DH keypair + nonces, and exposes the
  pixie bundle (PKE, PKR, E-Hash1/2, E-Nonce, AuthKey) after M3.
- `enrollee.py` (`WpsEnrollee`): the role-flipped machine for PBC.
- `pins.py`: the PIN keyspace (checksum, two-halves split, ordering, resume).
- `known_pins.py` / `wps_algos.py` / `wps_router_ouis.py`: per-OUI default-PIN candidates
  and the OUI-to-vendor lookup.
- `lock.py`: lock detection (the beacon AP-Setup-Locked IE plus a 3-strike M3-NACK
  heuristic for silent lockers) and adaptive backoff that learns per-router lock duration.

The Focus-facing orchestrators are one level up: `campaigns/pin.py` (`WpsCampaign`) drives
the PIN sweep, `campaigns/pbc.py` (`WpsPbcCapture`) drives PBC, and
`campaigns/auth_assoc.py` (`Association` + `WlanTransport`) is the shared auth/assoc engine.

## One PIN attempt

We act as the external Registrar; the AP is the Enrollee. One attempt is a full
EAP/EAPOL session:

```
TX  EAPOL-Start
RX  EAP-Request/Identity
TX  EAP-Response/Identity = "WFA-SimpleConfig-Registrar-1-0"
RX  M1   (Enrollee nonce N1, PKe = enrollee DH pubkey, device info)
TX  M2   (Registrar nonce N2, PKr, Authenticator)
        both sides now derive DHKey = SHA256(g^AB mod p);
        KDK = HMAC-SHA256_DHKey(N1 || EnrolleeMAC || N2);
        KDF(KDK) -> AuthKey(256) || KeyWrapKey(128) || EMSK(256)
RX  M3   (E-Hash1, E-Hash2)         <- pixie needs only up to here
TX  M4   (R-Hash1, R-Hash2, ENC{R-S1})
RX  M5   or NACK                    <- NACK here means the FIRST half of the PIN is wrong
TX  M6   (ENC{R-S2})
RX  M7   or NACK                    <- NACK means SECOND half wrong; M7 means SUCCESS
TX  WSC_NACK (tear down)
```

**The two-halves oracle** (why it is ~11k attempts, not 10^8): the 8-digit PIN splits
into PSK1 (first 4 digits) and PSK2 (last 4 = 3 digits + 1 checksum). M4 reveals our
R-S1; the AP answers M5 only if our guessed first half matches, else NACK. M6/R-S2 does
the same for the second half. So 10^4 (first half) + 10^3 (second half, the checksum
fixes the 8th digit) is about 11,000 worst case. The first/second-half-wrong signal comes
from an explicit WSC_NACK, an EAP-FAIL, or an M5/M7 timeout-as-NACK.

The WSC message must be bounded by the EAP length field, not the end of the frame.
Anything trailing the EAP packet (chip padding, hardware metadata) would otherwise leak
into the next Authenticator HMAC (`HMAC(authkey, M_prev || M_curr)`, which covers the raw
WSC bytes), and every M2 gets rejected with config-error 2
(`WPS_CFG_DECRYPTION_CRC_FAILURE`). `messages.parse_rx_frame` slices `[attrs_start :
e+eap_len]`.

## RX routing

The campaign registers an RX callback, filters to frames to/from our forged MAC + target
BSSID with ethertype `0x888e`, and feeds an `asyncio.Queue` the registrar awaits: the
low-latency path, bypassing the UI-polled registry.

## Hardware-verified behavior

Rebuilt from live ground-truth (`scripts/wps/wps_lab.py`) after the field showed false
"first half wrong" reports and missed cracks. Most prior heuristics turned out to be
flaky-TX artifacts, not real AP behavior:

- **Auto-ACK (active monitor) is harmful.** A drift-controlled A/B ran 5/7 vs 3/7 cracks.
  Arming HW auto-ACK kills the AP's retransmit safety net, so a dropped M5/M7 becomes
  permanent and scores as a wrong PIN. The retransmit "cost" is a non-issue: the AP's
  DH/M3 at a consistent ~1.24s dominates per-attempt time. The campaign does not arm
  active monitor.
- **One-shot-per-association is real WSC.** A second exchange on a kept-alive association
  is refused pre-oracle ("Device Password Auth Failure"), so the campaign re-associates
  per PIN (`_try` resets the session). One-shot-per-MAC is false (same-MAC reuse still
  cracks), so there is no proactive MAC rotation.
- **Association is the top failure (~29%).** `_send_until` resends auth/assoc while
  silent, 3 attempts grew to 5 with a longer auth window: the biggest single lever once
  auto-ACK was off.
- **Timeouts were too short** (M1 up to ~4.3s). EAPOL-Start grew from 2s to 7s, plus an
  in-session resend of the last frame on a per-stage timeout covers a dropped M2/M4/M6.
- **Some APs lock silently** (no config-error, no beacon flag). The 3-strike soft-lock in
  `lock.py` catches that; `config_error=15` adds the spec path for APs that signal it.
- Checksum-invalid commons (11111111 / 88888888 / 10000005) do not choke; that was TX
  loss too.

The live test AP cracks end-to-end at 1 attempt per PIN with no soft-lock churn. The
registrar/association/campaign path is driver-agnostic; validate each card with
`wps_lab.py`.

## Default-PIN candidates

Before the full sweep, `known_pins.py` tries per-vendor default-PIN generators
(`wps_algos.py`, dispatched by OUI via `wps_router_ouis.py`): the ComputePIN and Airocon
style algorithms plus the trivial cases, which are the majority of real hits on aging gear.

## PBC (push-button) capture

Opportunistically grab the PSK when someone opens a WPS Push-Button Configuration window
(about 120s, `WPS_PBC_WALK_TIME`). PBC inverts the roles: the AP is the Registrar
(button-pressed, holds the creds) and we are the Enrollee. The message polarity flips (the
AP sends WSC_Start/M2/M4/M6/M8, we send M1/M3/M5/M7/Done) and the PSK arrives in M8. EAP
request/response polarity is unchanged (the AP is always the authenticator), so the
framing is reused.

It must be active, not a passive eavesdrop: M8's credential is encrypted under
KeyWrapKey <- AuthKey <- DHKey <- g^(ab) mod p, and a passive listener has neither private
key. Recovering it is the Computational DH problem over the 1536-bit MODP group 5, i.e.
infeasible. The only path to the PSK is to be the enrollee that completes the handshake,
racing the legitimate device. On overlap we never self-abort (ignore
`MULTIPLE_PBC_DETECTED`); the AP still aborts true simultaneous overlaps, and we win by
finishing first.

Detection is passive and always-on: the walk window advertises Device Password ID = 0x0004
(PBC) + Selected Registrar = 1 in beacons/probe-resps, which drives
`AccessPoint.wps_pbc_active` (edge-triggered OFF->ON). Some APs only advertise it while the
button is held long enough to open the window, and some never advertise it, so
`pbc_probe.py --now` covers the blind case. Scanner arms via the `w` toggle (off /
selected / global); Focus auto-captures a window on its current target.

## Hard-MAC WPS gap

WPS (PIN + PBC) works on every firmware-based card but struggles on the oldest hard-MAC,
register-only parts (RTL8187L, RT2500USB): these have no hardware auto-ACK and different TX
timing, and the longer, more stateful WPS EAP exchange is more fragile over dozens of
frames per PIN than the short handshake/PMKID exchanges. Both cards pass deauth, handshake,
PMKID, and WEP, so association and injection work; only WPS is unreliable. Not yet
root-caused.

## Pixie-dust (planned)

Native PixieWPS (recovering the E-S1/E-S2 nonces from a weak PRNG so the half-PIN search
runs offline against the captured E-Hash1/2) is designed but not built. The crypto core in
`dot11/wsc/crypto.py` already exposes what it needs (`check_pin_half` and the pixie bundle
after M3). Revisit the numpy dependency for the time-seed/eCos seed search before building.
