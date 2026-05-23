# WEP attacks (and cracking)

Scope doc for porting `aireplay-ng -1/-3/-4/-5` + an `aircrack-ng`-equivalent PTW key recovery into wifit3. Native Python on top of `WlanInterface`, same shape as the existing PMKID / SAE / decloak attacks.

## Why

`NEXT-STEPS.md` used to say "skip WEP — outdated". Flipped 2026-05-22: WEP networks still occasionally surface (legacy IoT setups, ancient routers left running) and seeing one in Scanner with no attack available is the wrong end of the wifit3 promise. Modern isn't the only target.

## Tool taxonomy (refresher)

| `aircrack-ng` tool | wifit3 equivalent |
|---|---|
| `airodump-ng` — passive RX, IV collection | already-present Scanner / Focus, extended with IV dedup + count |
| `aireplay-ng` — active TX, IV generation | new — this directory |
| `aircrack-ng` — PTW / FMS / KoreK cracker | MVP shells out; full-native ports PTW |
| `airdecap-ng` — post-crack pcap decrypt | one-shot utility, not a TUI feature |

## Phase 1 — IV capture (~2 days, first M)

Listen for WEP-encrypted data frames on a target BSSID. Extract the 3-byte IV (offset 24 in the encrypted body, immediately after MAC header). Dedup + count uniques. Stash to either:
- in-memory ring buffer (live counter in Focus), and / or
- a `.cap` / `.ivs` file on disk for downstream cracking.

No TX required. Builds entirely on the existing parser RX path.

UI: WEP-encrypted AP in Scanner gets a `(W)` marker. Focus on it shows "X unique IVs / Y total frames" counter, ticking up live.

## Phase 2 — IV generation attacks (~2 weeks total)

Mirror `aireplay-ng`'s option flags so the existing documentation / muscle memory transfers cleanly.

### `-1` Fake authentication (~1 day) — prerequisite
Open-system authentication + association as a forged client so the AP accepts our future injections. Builds on the auth / assoc machinery already in `pmkid_harvest.py`. Lands first.

### `-3` ARP replay (~1–2 days) — the workhorse
Listen for an ARP request (broadcast WEP data frame, distinctive 68 / 86-byte size depending on padding). Once captured, replay it on a loop. The AP echoes back ARP replies — each carries a fresh IV. **Most WEP networks fall to this attack alone** given an existing client on the network.

### `-5` Fragmentation [Bittau 2005] (~3–5 days)
Inject fragmented frames with known plaintext (LLC / SNAP header — always the same prefix). The AP's response, when reassembled, reveals up to ~1500 bytes of keystream. With keystream we forge arbitrary packets — typically a fake ARP request to feed back into `-3`. Useful when no client is on the network to source the seed ARP.

### `-4` ChopChop [KoreK 2004] (~3–5 days)
Byte-by-byte decryption of one captured packet via the AP as an ICV oracle. Strip the last byte, XOR in a guess, send. AP either acks (correct guess) or doesn't. 256 guesses per byte max. Recovers plaintext + keystream without ever knowing the key. Slower than fragmentation; useful as a fallback when fragmentation doesn't elicit a response.

## Phase 3 — Cracking

### MVP path (~½ day)
Shell out to a system-installed `aircrack-ng` against the dump file, parse its stdout for the recovered key, surface in the UI. External dependency, but the user-visible flow is identical to the native path.

### Full-native path (~1 week)
Port PTW [Pyshkin / Tews / Weinmann 2007]. Needs ~40–85 k unique IVs for 104-bit recovery. Breakdown:
- RC4 inner loop: ~20 lines
- IV → key-byte vote tabulation: ~200 lines
- Search + key-candidate testing: ~200 lines
- FMS + KoreK fallback for stubborn cases: another ~500 lines

Net: ~500–1000 LoC of careful crypto math. Self-contained; no external `aircrack-ng` required.

## Milestones

```
M1  — Phase 1 IV capture + UI counter           (~2 days)
M2  — Fake auth (-1) on a forged client         (~1 day)
M3  — ARP replay (-3) end-to-end                (~2 days)
M4  — Shell-out PTW crack ("MVP scope")         (~½ day)   ← shippable
        ───── ship here for the satisfying flow ─────
M5  — Fragmentation (-5)                        (~4 days)
M6  — ChopChop (-4)                             (~4 days)
M7  — Native PTW                                (~5 days)
M8  — FMS + KoreK fallback                      (~3 days)
        ───── "full native" milestone ─────
```

M1–M4 in ~1 week of focused work lands the visible feature. M5–M8 close it out in another ~2–3 weeks.

## Testing

User has a box of ~2017-era routers — many of those still support WEP. Plan: dedicate one as a permanent WEP test target, leave it running while developing. WEP-active networks on the wider neighborhood scan are rare but possible (legacy IoT, forgotten setup APs).

## Open questions before M1 starts

- Does our existing RX path surface WEP-encrypted data frames cleanly, or do some chips auto-decrypt / drop them? Should sanity-check on each working driver (`ar9271`, `rt2800usb`, `rtl8821au`, `rtl8822bu`, `rtl8812au`, `rtl8188eus`, `mt76x2u`, `mt76x0u`) — monitor-mode promiscuous RX usually delivers the encrypted bytes verbatim, but worth confirming.
- IV-dump file format: stick with `.ivs` (aircrack native, compact) or `.cap` (pcap-format, inspectable in Wireshark, ties into existing pcap export plumbing)? Lean `.cap` for symmetry with our handshake export.
- Should chopchop / fragmentation actually land before M4 ships, or after? Argument for after: ARP replay covers ≥90% of WEP-with-client cases; the fallbacks are for edge cases that may never come up in real testing.
