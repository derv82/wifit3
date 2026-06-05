"""RTL8812AU (ALFA AWUS036ACH, 2T2R) — vendor/DKMS cleanroom port.

Sibling to the mainline-derived ``chips/rtl8812au/`` (kept as the env-var fallback).
Built on the shared ``chips/rtl88xxau_base/`` jaguar core (proven by the 8821au port)
plus the 8812a-specific deltas: 2T2R RF (RADIO_B + path-B everywhere), the 8812 band
switch, the ``_8812AUsb`` MAC variants, and the 2-path EFUSE/TX-power. Ported from the
same Lucid-Duck ``rtl88xxau`` vendor source as the 8821 (it implements 8812a in
``hal/rtl8812a/`` + ``hal/phydm/rtl8812a/``); no vendor 8812 cold-boot pcap exists, so
verification leans on the shared base's 8821 replay-diff + golden-hashed init tables +
a structural cross-check vs the mainline 8812 capture + live HW on the AWUS036ACH.
"""
