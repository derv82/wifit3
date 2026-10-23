# Wifit3: Known Bugs & QoL

Tracked defects and design debt. Each entry is a **problem statement**, not a prescribed
solution. The fix is whatever's simplest, tackled one at a time. Where a simple direction is
obvious it's noted in one line; the point is to *remove* leaky abstractions, never add layers.

## (CANTFIX) RTL8822BU TP-Link Archer T4U v3 / T4U+ ambiguity

**Problem.** TP-Link manufactured multiple different models that all share
the same exact device metadata.  A USB device with VID:PID `2357:0115` could be
Archer T4U v3, v3.2, or Archer T4U+. All other descriptors appear to be identical.

Known T4U+ sample (identical to T4U "v3.6" sample):
- `idVendor:idProduct`: `2357:0115`
- `manufacturer`: `Realtek`
- `product`: `802.11ac NIC`
- `serial`: `123456`
- `bcdDevice`: `0210`
- `bcdUSB/version`: `2.10`
- `speed`: `480`
- `chipset`: `RTL8822BU` (could be RTL8812BU given online reports)

Links:
- [linux-hardware.org@`2357:0115`](https://linux-hardware.org/?id=usb:2357-0115) (RTL8822BU, RTL8812BU)
- [Wi-Cat.ru@T4Uv3.2](https://wikidevi.wi-cat.ru/TP-LINK_Archer_T4U_v3.2) *("probably rtl8812bu")*
  - [Wi-Cat.ru `2357:0115` disambiguation](https://shorturl.at/5iTC7)

**Direction.** Keep dumping device info from other models; spot the diff.

## Expand EFUSE support

Most drivers were ported against the single device they were tested on, so EFUSE derived values
(antenna count, TX power tables) sit in the code as constants. A device whose EFUSE differs then
gets wrong values with no error raised: little or no RX/TX, or wedging.

Chipsets confirmed to honor EFUSE: RTL8821AU, RTL8822BU, RTL8822CU.

Direction: per driver, list the EFUSE fields the vendor driver reads, compare against the wifit3
driver, port what is missing. The vendor's per field parsers are uniformly named, so the field list
comes out of the vendored source directly:

    grep -rhoE 'Hal_EfuseParse[A-Za-z0-9_]+' <vendor-tree>/ | sort -u

On a tree that compiles several chips that reports more fields than the one chip uses. Narrowing it
to the chip's own efuse reader gives the exact set, e.g. for RTL8822CU:

    sed -n '/rtl8822c_read_efuse/,/^}/p' hal/rtl8822c/rtl8822c_ops.c | grep -oE 'Hal_[A-Za-z0-9_]+' | sort -u

## PixieDust second half assumes a valid PIN checksum

**Problem.** `_find_second_half` (`campaigns/wps/pixie.py`) only tries the 1000 checksum-valid
candidates, because `pins.full_pin()` always appends the computed 8th digit. Plenty of shipped
PINs carry a wrong checksum digit, and for those the offline attack recovers the first half,
fails the second, and reports "no PixieDust matches" on a PIN that is actually recoverable.

The online brute force has to make that assumption (one guess costs a round trip, so reaver
hard-codes `P1_SIZE 10000` / `P2_SIZE 1000` and builds every PIN as
`snprintf(pin, len, "%s%d", key, wps_pin_checksum(atoi(key)))` in `src/pins.c:build_wps_pin()`).
Offline it is free: ~40ms for the extra halves.

**Direction.** Mirror pixiewps `src/pixiewps.c:crack_second_half()` — after the 1000
checksum-valid halves, sweep all 10000 four-digit halves, skipping those already tried:

    for (second_half = 0; second_half < 10000; second_half++) {
        if (wps_pin_valid(first_half * 10000 + second_half)) continue;  /* already tested */
        uint_to_char_array(second_half, 4, s_pin);
        if (check_pin_half(&hc, s_pin, psk, wps->e_s2, wps, wps->e_hash2)) ...
    }

`wps_pin_valid(pin)` is `wps_pin_checksum(pin / 10) == (pin % 10)`; `pins.pin_checksum()` is
already the same function. Needs a `pins` helper that emits an 8-digit PIN *without* forcing the
checksum, since `full_pin()` cannot express these.

## PixieDust never tests the empty PIN half

**Problem.** An enrollee configured with a zero-length device password hashes the empty string
into PSK1/PSK2. `_find_first_half` / `_find_second_half` start at `"0000"`, so that AP is never
recovered even though it is the cheapest case of all (two HMACs).

**Direction.** Mirror pixiewps `src/pixiewps.c:check_empty_pin_half()`, tried first for *both*
halves (`crack_first_half` / `crack_second_half` call it before their loops). The PSK is the
HMAC of the empty message, `empty_psk = HMAC-SHA256_AuthKey("")` (pixiewps `empty_pin_hmac()`),
fed through the normal hash: `E-Hash = HMAC_AuthKey(E-S || empty_psk[:16] || PKe || PKr)`.
`wsc/crypto.check_pin_half(..., pin_half_ascii=b"")` already computes exactly this, so the fix is
one call per half ahead of each loop, plus a PIN representation for "empty" in `PixieResult`.
Reaver has no equivalent — an online attack cannot test it.

## PixieDust RTL819x throws away a recovered first PIN half

**Problem.** `_recover_rtl819x` (`campaigns/wps/pixie.py`) finds the E-S1 seed, then sweeps ten
forward seeds for E-S2. If none match it returns `None`, discarding first-4 digits it has already
*proved* correct against E-Hash1 — so the campaign goes on to re-brute-force that same half online,
thousands of live attempts for something already known. Reproduces whenever the enrollee's E-S2
seed lands outside the +0..+9s window, e.g. exactly +10s after E-S1's.

**Direction.** Let a partial result escape: carry the known first half out of PixieDust and have
the campaign enter its second-half phase with that half locked, as it already does after a live
`FIRST_HALF_OK`. Needs a field on `PixieResult` and a branch in `_try_pixie`.

## PixieDust glibc seeding is modelled as unsigned

**Problem.** `pixie_prng.glibc_nonce` keeps the seed unsigned, so it diverges from glibc for seeds
>= 2^31: `glibc_nonce(2147483648)` yields `2b101346...`. Unix timestamps cross 2^31 in **January
2038**, after which RTL819x recovery silently stops working. Also reachable today only via a
caller-supplied `rtl_window` low enough that `nonce_seed - dist` goes negative.

**Direction.** Establish what glibc actually does first: `__srandom_r` assigns the seed to an
`int32_t` state but runs Schrage's step through a `long int`, so the answer differs between 32- and
64-bit builds. Settle it against a real glibc on both, then match it and add a known-answer vector
above 2^31. Clamp or reject negative seeds either way.

## PixieDust eCos mode mishandles a high nonce byte

**Problem.** `ecos_simple_recover` computes the known seed bits as `(e_nonce[0] << 25) & 0xffffffff`,
which discards bit 7 of the byte. Any enrollee whose nonce starts >= 0x80 makes it sweep the wrong
2^25 space and return `None`. `ecos_simple_model` only ever emits `nonce[0] < 0x80`, so the round
trip is self-consistent and the tests cannot see it. `ECOS_SIMPLE` is experimental and not in
`DEFAULT_MODES`, so nothing ships broken today.

**Direction.** Work out what the device really does with the top bit — whether the nonce byte is
the seed's top 7 bits or its top 8 with one bit lost to the shift — before switching the mode on.

## PixieDust first-half search recomputes PSK1 for every candidate seed

**Problem.** `check_pin_half` derives `PSK1 = HMAC(authkey, half)[:16]` on every call, so the
RTL819x sweep recomputes the same 10000 PSKs for each of its 1201 candidate E-S1 values — half of
all HMAC work in the heaviest mode. Measured 49s, against 19s when the PSK table is built once
and reused (2.5x), on a high-end x86 laptop.

**Direction.** Build the 10000 PSK1 (and 1000 PSK2) halves once per `recover_pin` call and compare
`HMAC_AuthKey(E-S || psk || PKe || PKr)` directly, instead of going through `check_pin_half` per
candidate. Keep `check_pin_half` as-is: it is the hostapd-shaped primitive the online path uses.

## RTL8188EUS and RTL8821AU (DKMS) bring-up is flaky with several cards attached

**Problem.** Both DKMS drivers fail `connect()` intermittently when more than one device is
plugged in. The RTL8821AU raised `USBError [Errno 2] Entity not found` from the first vendor
control transfer on 3 of 8 consecutive bring-ups with two cards attached, each time followed by
`[8821au-dkms-rx] device gone: [Errno 19] No such device`; the device stayed enumerated on the
bus throughout and the next attempt succeeded. See also the RTL8188EUS flake, where `connect()`
returns True on a dead handle.

**Direction.** Deeper dive needed. Establish first whether the failure is contention between two
cards' bring-up sequences or a per-device cold-boot race, by bringing the same card up alone
repeatedly versus alongside a second card.

## CPU Crack has no nonce error correction

**Problem.** hashcat applies nonce error correction (+/-8 on the ANonce) to EAPOL hashlines; CPU
Crack (`vault/tools/cpu_crack.py`) does not. A rare handshake with a drifted nonce cracks in
hashcat and is missed by CPU Crack.

Measured: correction costs 1.09x (one byte order) to 1.19x (both) CPU per password; 70 of 71 local
EAPOL lines have the NC bit set. Not implemented: no real-world miss observed yet.

## Vault Verify from Focus leaves the card on the AP's channel

**Problem.** In Focus, open the Vault, select a cracked AP on a different channel than the Focus
target, press `Verify`: the card retunes to that AP's channel to verify, but doesn't return to the
Focus channel. The Focus target's beacons stay at 0 until the Vault is closed, then come back.

## PSK-SHA384 and FT-PSK-SHA384 handshakes are not yet supported

**Problem.** AKM 20 (PSK-SHA384) and AKM 19 (FT-PSK-SHA384) derive the KCK and MIC with SHA-384
(24-byte MIC). `-m 22000` has no SHA-384 path in released hashcat, so wifit3 withholds these
captures as "not yet supported". Hashcat mainline added one ([hashcat#4931](https://github.com/hashcat/hashcat/issues/4931)).

**Direction.** Once a hashcat release ships it, add the hashline format and drop the withhold.

## WPA-Enterprise credentials are not yet supported

**Problem.** An Enterprise 4-way is keyed by a per-session PMK from EAP and is never crackable.
The crackable secret is the user's credential: an EAP-MD5 challenge/response (`-m 4800`) sent in
the clear, or an MSCHAPv2 response (`-m 5500`) that PEAP/TTLS hide inside TLS, so it takes a rogue
AP (an Enterprise EvilTwin). Vault, the cracker and capture storage all assume `.hc22000`.

**Direction.** A new capture type through Vault and the cracker; scope it before starting.
