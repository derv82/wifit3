"""RTL8812AU (ALFA AWUS036ACH, 2T2R) — vendor/DKMS cleanroom port.

Sibling to the mainline-derived ``chips/rtl8812au/``. The mainline driver is the default
for 0bda:8812; ``WIFIT3_RTL8812=dkms`` selects this port (until an A/B proves it matches
or beats mainline). Built on the shared ``chips/rtl88xxau_base/`` jaguar core (proven by
the 8821au port) plus the 8812a-specific deltas: 2T2R RF (RADIO_B + path-B everywhere),
the 8812 band switch, the ``_8812AUsb`` MAC variants, and the 2-path EFUSE/TX-power. The
cold-boot bring-up is verified **byte-for-byte** against morrownr's 8812au USB captures
(capture-2 and capture-3) and confirmed by live RX off the antenna; ``hal/rtl8812a/`` +
``hal/phydm/`` are the vendor C source. See ``RTL8812AU_DKMS.md`` for the verified ground
truth and the byte-for-byte gate.
"""
