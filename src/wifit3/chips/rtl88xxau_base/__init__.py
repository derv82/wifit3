"""Shared RTL88xxAU (jaguar / 88xxA) vendor-DKMS infrastructure.

Chip-agnostic code factored out of the proven ``rtl8821au_dkms`` port so a second
88xxA chip (RTL8812AU, 2T2R) can reuse it without a fork. This base holds ONLY the
zero-delta core — the parts byte-identical across the family:

  * ``transport``  — Realtek rtw88-family vendor control transfers + bulk RX/TX.
  * ``registers``  — family-shared MAC/PHY register addresses + bit constants.
  * ``sipi``       — SIPI BB/RF register I/O primitives (path-parameterised).
  * ``phy_cond``   — JaguarSeries IF/ELSE table walker (ODM_ReadAndConfig).
  * ``pwrseq``     — HalPwrSeqCmd runtime (per-chip TABLES live in the chip pkg).
  * ``firmware``   — power-on / LLT / FW-download mechanics (blob+flow injected).
  * ``efuse``      — EFUSE byte-read + PG logical-map walk (per-chip PARSE in pkg).
  * ``rx``         — RX-desc decode + aggregated bulk-IN walk (RSSI fn injected).
  * ``tx``         — the ``rtl8812a_fill_fake_txdesc`` builder (already 88xxA-wide).

The chip-shaped modules (mac/bb/chan/txpower/dig/monitor + the efuse parse + the
RSSI formula + every init table) stay per-chip and are NOT shared here — they
differ enough (RF path count, RFE registers, per-chip PHYDM values) that one
parameterised copy would be less honest than two separate ports. The mainline
precedent for a family base is ``chips/rtw88_base/``.

``chips/rtl8821au_dkms/`` is intentionally NOT migrated onto this base: it is the
shipped, replay-diff-proven driver and is kept frozen with its own copies. This
base's first consumer is ``chips/rtl8812au_dkms/``.
"""
