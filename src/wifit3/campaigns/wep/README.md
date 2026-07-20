# WEP attacks

Native WEP auditing on top of `WlanInterface`: fake-auth, ARP replay, ChopChop, and
PTW key recovery. Same shape as the PMKID / SAE / decloak attacks, no `aireplay-ng`
subprocess. Fragmentation (`-5`) was removed (see the end); `fragmentation.py` is kept
as a dormant reference.

## Tool taxonomy

| `aircrack-ng` tool | wifit3 equivalent |
|---|---|
| `airodump-ng` (passive RX, IV collection) | Scanner / Focus, with IV dedup + count |
| `aireplay-ng` (active TX, IV generation) | this directory |
| `aircrack-ng` (PTW / FMS / KoreK) | native PTW in `crack/wep.py` |
| `airdecap-ng` (post-crack pcap decrypt) | one-shot utility |

## The attacks

**IV capture** (passive, no TX). The parser RX path surfaces WEP-encrypted data
frames; `wlan/wep_store.py` dedups the 3-byte IV (offset 24, right after the MAC
header) and counts uniques, which drives the Focus "ETA to 10k" line. A WEP AP shows
a `(W)` marker in Scanner.

**Fake auth (`-1`).** Open-system auth + assoc as a forged client so the AP accepts
our injections. Built on the auth/assoc machinery in `campaigns/auth_assoc.py` (shared
with PMKID and WPS). Prerequisite for the TX attacks.

**ARP replay (`-3`).** Capture a broadcast ARP request (a distinctively-sized WEP data
frame) and replay it on a loop; each AP echo carries a fresh IV. Most WEP networks with
an active client fall to this alone.

**ChopChop (`-4`)** [KoreK 2004]. Byte-by-byte decryption of one captured frame using
the AP's ICV check: chop the last byte, guess its plaintext (up to 256 tries), fix
the ICV (CRC32 is linear), send. The AP relays the frame iff the guess was right.
Recovers plaintext + keystream without the key, then forges a broadcast ARP to feed
replay. Used when no client traffic sources a seed ARP.

## Cracking

The MVP shells out to a system `aircrack-ng`. The native path (`crack/wep.py`) ports
PTW [Pyshkin / Tews / Weinmann 2007]: ~40-85k unique IVs for 104-bit recovery, via an
RC4 inner loop, IV-to-key-byte vote tabulation, and candidate search, with an FMS +
KoreK fallback for stubborn cases. Self-contained, no external cracker.

## Attack coordination

ARP replay is the IV engine and home base. Frag and Chop are two alternative
"manufacture an ARP seed" sub-modes for when replay has no ARP to work with (no client
traffic). All three are mutually-exclusive TX activities on one half-duplex radio, so
`WepArpReplay` exposes `pause()` / `resume()` and the campaign owns the "one TX activity
at a time" invariant.

Transitions are user-driven, with exactly one automatic transition: success (which is
unambiguous). "No response" never is (out of range vs a TX glitch vs a genuinely immune
AP), so the code never auto-advances or auto-stops on a failed round. It keeps retrying
the chosen mode and logs a running tally ("ChopChop: byte 4/36") for the user to judge.

```
Generate IVs -> REPLAYING (fake-auth underneath; waits for / replays ARPs)
   user clicks Frag/Chop -> pause replay -> FRAGMENTING / CHOPPING
     (clicking the other mode stops the current one: click-to-switch)
   FRAGMENTING/CHOPPING --success (keystream -> forged ARP)--> resume REPLAYING
   FRAGMENTING/CHOPPING --round fails--> keep retrying same mode (never auto-stop)
   Stop IVs -> tear down
```

Focus shows **Frag** and **Chop** as always-enabled Start/Stop toggles alongside the
running campaign; clicking one stops the other.

## ChopChop relay signature (hardware-verified)

The RX signature `chopchop.py` matches for an accepted guess: a Data frame, FromDS +
Protected, Addr1 (DA) = broadcast, Addr3 (SA) = our forged STA MAC, length == original
minus one. Match on SA, not BSSID: one correct guess produces two relays (the frame
echoed onto sibling BSSes, a fresh IV each), so de-dup. Per-guess relay latency is about
3 ms, so the relay timeout is ~20 ms; each guess goes out 2-3 times since single no-ACK
sends can be lost. `chopchop.py` chops the variable tail (positions 16..39; ks[0..15]
come from the known SNAP + ARP-request prefix), recovers ks[i] = body[i] ^ accepted-guess
per step, forges a broadcast ARP from the keystream, and hands it to the campaign as a
replay seed.

## ARP-replay rate control

Each 1-second window injects `rate` packets in one burst at the card's full speed, then
sleeps out the rest of the second (a single ~1s sleep, immune to the ~15ms timer
granularity that sub-cycle pacing fought). Perturb-and-observe on `rate`: if this
window's IVs/s beat the last, keep the step direction, else reverse, so the rate walks
toward the AP's real ceiling and dithers there with no low-ceiling assumption. P&O acts
on an EWMA of IVs/s, not the raw window: briefly lowering the rate spikes captured IVs/s
as the AP's queue flushes, which the raw signal would misread as "lower is better", and
the EWMA damps that transient. The objective is IVs/s (proportional to time-to-crack),
not pps and not capture%. A fixed-rate mode is the fallback if P&O misbehaves.

## Fragmentation, removed (2026-06-02)

Fragmentation (`-5`) needed every fragment of an MSDU to share one 802.11 sequence
number, which meant injecting with `en_hwseq=0` so the chip does not auto-assign it: a
software-sequence TX path only ever wired on the RTL8821AU. On every other card each
fragment got a different hardware sequence, the AP never reassembled, and the daemon
spun on "seed would not relay." One card owning one attack was a maintenance smell, and
ARP replay + ChopChop carry the suite, so the sw-seq plumbing (`SUPPORTS_SW_SEQ`,
`en_hwseq=0`, `build_tx_desc_data`, `send_raw(sw_seq=)`) was removed and the Frag button
unhooked. `fragmentation.py` is kept as a dormant reference (it will not run as-is).
Re-introducing it should add shared sequence-ID support to the TX framework, not
recreate a per-driver special case.
