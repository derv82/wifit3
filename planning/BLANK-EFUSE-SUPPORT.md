# Wifit3 — Blank-EFUSE detection + support

A card whose EFUSE/EEPROM reads **blank** (identity bytes programmed but the RF/cal region all
`0xFF`, `NIC_CONF0 == 0`) is almost always **counterfeit silicon** — the factory never burned
its per-unit RF/power calibration. wifit3 should (1) **tell the user** (no other tool does),
and (2) optionally **substitute a sane generic image in driver RAM** so the card behaves less
stupidly. **We never write fuses** — EFUSE is one-time-programmable (bits blow `0→1`
permanently); a wrong burn bricks the card or sets an illegal RF/regulatory state with no undo.

---

## 1. Detect + warn the user (the kind feature)

On bring-up, after the EFUSE read, classify it: **healthy** vs **blank/counterfeit** (identity
present but RF/cal region `0xFF` / `NIC_CONF0 == 0`). If blank, surface a clear heads-up in the
UI before continuing — roughly:

> ⚠️ This card's EFUSE/EEPROM is blank — it's likely **counterfeit**. RF calibration was never
> burned, so RX/TX may be weak. Continue? **[Y/n]**  (optionally: "try a generic EFUSE? — §2")

Show it **every boot** for an affected card (the user may forget which unit is unburned) —
it's information, not nagging. This is a genuinely kind feature; no tool told the user their
card was fake.

## 2. In-RAM override (never burn)

**Soft override only.** Replace the values the driver reads into RAM at init — `efuse.py`
already parses the EFUSE into a struct; feed it our image instead when blank. Fully reversible,
zero hardware risk. (Subsumes the deferred "93C66 EEPROM fallback" in `RT2800USB.md` — same
need, one mechanism.)

**Design (discuss class shape with lead before coding):** an `EepromOverride` source in
`efuse.py` that detects blank, loads a 512-byte image, and produces the *same* `EepromValues`
the normal parser yields. Gate behind an **explicit flag / CLI opt-in** so it never silently
fakes calibration on a healthy card — surfacing fake cal as real is worse than a known-weak
card. Image provenance: kernel `rt2800` defaults, or a dump from a genuine RT3572 if acquired.

**Honest expectations (a gamble, worth building either way):**
- **TX should improve** — power is stuck at the low fallback (`RFCSR12=0x6b`, max attenuation)
  *because* the EFUSE reads blank; a good image with real `default_power` lifts it.
- **Per-unit RF cal can't be faked** — crystal/freq trim + power cal are measured per die at
  the factory. A generic image is *better than blank* but has the wrong trim for this die.
- **RX is the open question** — the rx-filter cal is a *runtime* loopback sweep (RFCSR24/BBP55),
  not an EFUSE value, and it **rails** on the counterfeit unit. Either (A) the blank EFUSE
  mis-configures the front-end earlier → loopback dies → rail (a good image might revive RX), or
  (B) the front-end is just bad and no image helps. Unknown until tested.
- **Worst case:** counterfeit silicon doesn't respond and nothing changes.

**Experiment:** inject a plausible image into the RAM struct on the RT3572, A/B the beacon rate
+ deauth strength vs blank — user-driven (try it with and without, see the diff). Low cost,
real learning; builds the genuine-no-EFUSE-card feature either way. If it meaningfully rescues
the unit, re-run the matrix and reconsider the demotion.
