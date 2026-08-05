"""RTL8812AU (ALFA AWUS036ACH, 2T2R) — vendor/DKMS cleanroom port.

Sibling to the mainline-derived ``chips/rtl8812au/``. **This DKMS port is the default for
0bda:8812** — it survives the 2.4+5 GHz channel hop that RF-synth-wedges the mainline
driver (A/B-proven on hardware); ``WIFIT3_RTL8812=mainline`` falls back to the mainline
driver. Built on the shared ``chips/rtl88xxau_base/`` jaguar core (proven by
the 8821au port) plus the 8812a-specific deltas: 2T2R RF (RADIO_B + path-B everywhere),
the 8812 band switch, the ``_8812AUsb`` MAC variants, and the 2-path EFUSE/TX-power. The
cold-boot bring-up is verified **byte-for-byte** against morrownr's 8812au USB captures
(capture-2 and capture-3) and confirmed by live RX off the antenna; ``hal/rtl8812a/`` +
``hal/phydm/`` are the vendor C source. See ``RTL8812AU_DKMS.md`` for the verified ground
truth and the byte-for-byte gate.
"""
from wifit3.models.device_id import DeviceID

from .constants import USB_PID_AWUS036ACH, USB_VID_REALTEK

SUPPORTED_IDS = [
    DeviceID(USB_VID_REALTEK, USB_PID_AWUS036ACH, "RTL8812AU",
             product_name="ALFA AWUS036ACH"),
]


def import_driver():
    from .driver import Rtl8812auDkmsDriver
    return Rtl8812auDkmsDriver
