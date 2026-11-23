# Default PSKs

Many routers ship with a WPA passphrase derived from their BSSID, SSID or serial number. wifit3 tries
these before the SSID variants when "This AP's SSID variants" is ticked. The algorithms live in
[`src/wifit3/crack/default_psks.py`](../src/wifit3/crack/default_psks.py); its docstring has the rules
for adding one (signature, matching, cost limits, version bump).

Implemented: `bssid_as_psk`: the BSSID without colons, then its last 10 hex digits (Axtel, Megared) and
last 8 (Tplink), each upper and lower case, on every AP.

The schemes below are mostly from routers sold between 2008 and 2016.

## Techniques

| Technique | How it works | Examples | Candidates | Cost |
|---|---|---|---|---|
| BSSID as the key | The MAC or its last 8–10 hex digits, sometimes behind a fixed prefix | Ote, Axtel, Megared, Tplink, Infostrada | 1 | none |
| MAC digit shuffling | Fixed rearrangement, nibble arithmetic or table lookups on the MAC's hex digits, sometimes of MAC+1 or MAC+2 | Arcadyan, HG824x, Wlan6, Dlink, Belkin | 1–24 | none |
| Hash of the MAC or SSID | MD5, SHA-1 or SHA-256 over the MAC or SSID plus a fixed string, cut and re-encoded | AliceGermany, ArnetPirelli, PBS, SkyV1, Zyxel, Pirelli, Tecom | 1 | 1 hash |
| Enumerated digits | A few unknown digits of the key are tried in full | Speedport500 (3 digits), Comtrend (2 × 256 MD5s) | 512–1000 | 1 PBKDF2 each |
| Serial number search | The key is a hash of the serial number; the SSID holds a few bytes of a hash of that serial, so every possible serial is hashed and only those that match the SSID are kept | Thomson, UPC | ~0–tens | 21.8M SHA-1s (Thomson), 10^7-step search (UPC) |
| Lookup table | A table maps an SSID or MAC range to a base value that feeds the key | AliceItaly, Cyta, CytaZTE, NetFaster, TeleTu, OteHuawei | 0–22 | 1 lookup |
| Fixed or SSID-only | The same key on every unit, or a key built from the SSID alone | Andared, CabovisaoSagem, Discus | 1–4 | none |

## Schemes

"Applies to" is the condition checked before generating anything. OUI counts are the number of
3-byte MAC prefixes the scheme matches.

| Scheme | Applies to | Inputs | Candidates | Cost | Table |
|---|---|---|---|---|---|
| AliceGermany | OUI 00:1E:40, 00:25:5E | MAC | 1 | 1 MD5 | |
| AliceItaly | SSID `Alice-` + 8 digits, prefix in table | SSID, MAC | 1–22 | ≤ 2 SHA-256 per row | 10 KB |
| Andared | SSID `Andared` | none | 1 (raw 64-hex PMK) | none | |
| Arcadyan | 13 OUIs (EasyBox / Arcor / Vodafone) | MAC | 1–2 | none | |
| ArnetPirelli | 21 OUIs | MAC | 1 | 1 SHA-256 | |
| Axtel | SSID `AXTEL[-XTREMO]-` + MAC tail | MAC | 1 | none | |
| Belkin | SSID `[Bb]elkin[._]` + hex, or 3 OUIs | SSID, MAC | 1–24 | none | |
| CabovisaoSagem | SSID `Cabovisao-` + 4 hex | SSID | 4 | none | |
| Comtrend | SSID `WLAN_` / `JAZZTEL_` + 4 hex, 12 OUIs | SSID, MAC | 1 or 512 | 1 or 512 MD5s | |
| Conn | SSID `CONN-X…`, or 15 OUIs | MAC | 1–2 | none | |
| Cyta | 8 OUIs, MAC prefix in table | MAC | 0–2 | none | 2 KB |
| CytaZTE | 8 OUIs | MAC | 0–few | none | 0.3 KB |
| Discus | SSID `Discus-` / `Discus--` + 6 hex | SSID | 1 | none | |
| Dlink | SSID `DLink-` / `dlink-` + 6 hex, or 29 OUIs | MAC | 1 | none | |
| Eircom | SSID `eircom` + 8 octal digits | MAC or SSID | 1 (WEP-104) | 1 SHA-1 | |
| HG824x | 61 Huawei OUIs | MAC | 1 | none | |
| Huawei | SSID `INFINITUM` + 4, or 179 OUIs | MAC | 1 (WEP-40) | none | |
| Infostrada | SSID `InfostradaWiFi-` + 6 | MAC | 1 | none | |
| InterCable | SSID `InterCable…`, BSSID `00:15` / `00:1D` | MAC | 2 | none | |
| Maxcom | SSID `MAXCOM` + 4 | MAC | 1 | none | |
| Megared | SSID `Megared` + MAC tail | MAC | 1 | none | |
| MeoPirelli | same 21 OUIs as ArnetPirelli | MAC | 1 | 1 SHA-256 | |
| NetFaster | OUI 00:05:59 | MAC | 0–few | none | 3 KB |
| Ono | SSID `P1xxxxxx0000x` | SSID | 2 (WEP) | 1 MD5 | |
| Ote | SSID `OTE` + 6 hex | MAC or SSID | 1 or 5 | none | |
| OteBAUD | SSID `OTE` + 4 hex, OUI 00:13:33 | MAC | 1 | none | |
| OteHuawei | SSID `OTE…`, BSSID `E8:39:DF:F5/F6/FD` | MAC | 0–1 | 1 lookup | 1.35 MB |
| PBS | SSID `PBS-` + 6 hex, or 15 OUIs | MAC | 1 | 1 SHA-256 | |
| Pirelli | SSID `FASTWEB-1-` + one of 14 OUIs + 6 hex | SSID | 1 | 1 MD5 | |
| Ptv | SSID `PTV-` / `ptv` / `ptv-` + 6 | MAC | 1 | none | |
| Sitecom2100 | OUI 00:0C:F6, 64:D1:A3 | MAC | 1 | 1 MD5 | |
| SitecomWLR341_400x | SSID `sitecom` + 6 hex, or the same 2 OUIs | MAC or SSID | 9 (18 without BSSID) | none | |
| SitecomX500 | OUI 00:0C:F6, 64:D1:A3 | MAC | 4 | none | |
| SkyV1 | SSID `SKY` + 5 digits, 16 OUIs | MAC | 1 | 1 MD5 | |
| Speedport500 | SSID `WLAN-` + 6 hex, 3 OUIs | SSID, MAC | 1000 | none (1000 PBKDF2s to try) | |
| Tecom | SSID `TECOM-AH4021-` / `AH4222-` + 6 | SSID | 1 | 1 SHA-1 | |
| TeleTu | SSID `teletu…`, MAC in table range | MAC or SSID | 1 | none | 2 KB |
| Telsey | SSID `FASTWEB-1/2-002196/00036F` + 6 hex | MAC or SSID | 1 | 128 lookup3 hashes | |
| Thomson | SSID one of 19 prefixes + 6 hex | SSID | ~0–3 | 21,835,008 SHA-1s | |
| Tplink | OUI F8:D1:11 | MAC | 1 | none | |
| UPC | SSID `UPC` + 5–8 digits, or OUI 64:7C:34 | SSID, MAC, band | ~tens | 10^7-step search + MD5s | |
| Verizon | 5-character SSID, 10 OUIs | SSID, MAC | 1 | none | |
| WifimediaR | SSID `wifimedia_R-` + 4 | MAC | 1–2 | none | |
| Wlan2 | SSID `WLAN_` + 2 hex, 5 OUIs | SSID, MAC | 1 (WEP-104) | none | |
| Wlan6 | SSID `WLAN` / `WiFi` / `YaCom` + 6 | SSID, MAC | 10 | none | |
| Zyxel | SSID `WLAN_` / `JAZZTEL_` + 4 hex, 3 OUIs | SSID, MAC | 1 | 1 MD5 | |

Not included: AlcatelLucent, whose key comes from an online lookup service, not an offline algorithm.

## Things to decide per scheme

- **Generation cost.** Thomson and UPC are too slow to run when the cracker modal opens. UPC's inner
  loop can be solved directly instead of searched; Thomson has no shortcut short of a precomputed index
  (about 66 MB).
- **Candidate count.** Speedport500 and Comtrend add 1000 and 512 PBKDF2s to every matching AP.
- **Not WPA passphrases.** Eircom, Huawei, Ono and Wlan2 produce WEP keys, and Andared a raw 64-hex PMK.
  `default_psks` drops these, so the scheme is useless unless wifit3 cracks WEP or tries PMKs directly.
- **Tables.** Six schemes need a data table (sizes above). Their redistribution terms are unknown.
- **Existing implementations have bugs.** Generating test vectors by running an existing implementation
  reproduces its bugs. Bugs seen so far:
  - AliceItaly: the table file starts with a byte-order mark, so `Alice-995xxxxx` never matches.
  - Ono: the SSID is only incremented when its last digit is 9.
  - Verizon: the with-BSSID branch takes the wrong MAC characters.
  - Ote: the no-BSSID candidates are 10 characters long.
  - UPC (Ubee): MAC bytes are padded with spaces, and an SSID number starting with 0 is read as octal.
  - Eircom: without a BSSID, small octal values crash the matcher.
  - SitecomX500: fails when the last 3 MAC bytes are all letters.
