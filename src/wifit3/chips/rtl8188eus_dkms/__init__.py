"""RTL8188EUS DKMS (vendor) port — scaffold.

Sibling vendor port of ``chips/rtl8188eus`` (mainline). Cleanroom port of the
``realtek-rtl8188eus`` 5.3.9 DKMS driver (phydm/ODM RX stack) for hotter, more
stable 2.4 GHz monitor RX. See ``RTL8188EUS_DKMS.md`` for the A/B justification +
coordinates. Port not started — the fresh session adds driver.py / transport.py /
constants.py / firmware.py / the calibration + RX/TX modules.
"""
