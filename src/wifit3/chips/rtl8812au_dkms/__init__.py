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
truth and the byte-for-byte check.

VID:PID set kept in lockstep with the mainline sibling ``chips/rtl8812au`` (all one silicon).
"""
from wifit3.models.device_id import DeviceID
from wifit3.chips.products import ALFA, ASUS, AboCom, AmpedWireless, Belkin, Buffalo, DLink, Edimax, EnGenius, Hawking, IODATA, Linksys, Logitec, NEC, Netgear, Planex, Sitecom, TPLink, TRENDnet, Tenda, WD, WistronNeWeb, Zyxel

_IDS = (
    (0x0BDA, 0x8812, "RTL8812AU", None, ALFA.AWUS036ACH),
    (0x0BDA, 0x881A, "RTL8812AU", None, WistronNeWeb.DAUK_W8812),
    (0x0BDA, 0x881B, "RTL8812AU", None, None),
    (0x0BDA, 0x881C, "RTL8812AU", None, None),
    (0x0409, 0x0408, "RTL8812AU", None, NEC.ATERMWL900U),
    (0x0411, 0x025D, "RTL8812AU", None, Buffalo.WI_U3_866D),
    (0x04BB, 0x0952, "RTL8812AU", None, IODATA.WN_AC867U),
    (0x050D, 0x1106, "RTL8812AU", None, Belkin.F9L1106V1),
    (0x050D, 0x1109, "RTL8812AU", None, Belkin.F9L1109V1),
    (0x0586, 0x3426, "RTL8812AU", None, Zyxel.NWD6605),
    (0x0789, 0x016E, "RTL8812AU", None, Logitec.AC866),
    (0x07B8, 0x8812, "RTL8812AU", None, AboCom._802_11AC),
    (0x0846, 0x9051, "RTL8812AU", None, Netgear.A6200_V2),
    (0x0B05, 0x17D2, "RTL8812AU", None, ASUS.USB_AC56),
    (0x0DF6, 0x0074, "RTL8812AU", None, Sitecom.AC1200),
    (0x0E66, 0x0022, "RTL8812AU", None, Hawking.HD65U_22),
    (0x1058, 0x0632, "RTL8812AU", None, WD.MYNET),
    (0x13B1, 0x003F, "RTL8812AU", None, Linksys.WUSB6300),
    (0x148F, 0x9097, "RTL8812AU", None, AmpedWireless.ACA1),
    (0x1740, 0x0100, "RTL8812AU", None, EnGenius.EUB1200AC),
    (0x2001, 0x330E, "RTL8812AU", None, DLink.DWA_183),
    (0x2001, 0x3313, "RTL8812AU", None, DLink.DWA_182_REV_B),
    (0x2001, 0x3315, "RTL8812AU", None, DLink.DWA_182),
    (0x2001, 0x3316, "RTL8812AU", None, DLink.AC1200),
    (0x2019, 0xAB30, "RTL8812AU", None, Planex.GW_900D),
    (0x20F4, 0x805B, "RTL8812AU", None, TRENDnet.TEW_805UB),
    (0x2357, 0x0101, "RTL8812AU", None, TPLink.ARCHER_T4U),
    (0x2357, 0x0103, "RTL8812AU", None, TPLink.ARCHER_T4UH),
    (0x2357, 0x010D, "RTL8812AU", None, TPLink.ARCHER_T4U_V2),
    (0x2357, 0x010E, "RTL8812AU", None, TPLink.ARCHER_T4UH_V2),
    (0x2357, 0x010F, "RTL8812AU", None, TPLink.ARCHER_T4UHP),
    (0x2357, 0x0122, "RTL8812AU", None, TPLink.ARCHER_T4UHP_V2),
    (0x2604, 0x0012, "RTL8812AU", None, Tenda.U12),
    (0x7392, 0xA822, "RTL8812AU", None, Edimax.EW_7822UAC),
)

SUPPORTED_IDS = [
    DeviceID(vid, pid, chipset, vendor, product)
    for (vid, pid, chipset, vendor, product) in _IDS
]


def import_driver():
    from .driver import Rtl8812auDkmsDriver
    return Rtl8812auDkmsDriver
