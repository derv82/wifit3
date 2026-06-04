"""RTL8821AU (RTL8811AU) — vendor/DKMS port (Realtek PHYDM/ODM stack).

Cleanroom re-port from the Lucid-Duck ``8821au-20210708`` 5.12.5.2 vendor source
(the DKMS-distributed out-of-tree rtl88xxau driver), NOT mainline ``rtw88``. Lives
beside the mainline-derived ``chips/rtl8821au/`` as a sibling; both register for
0bda:0811 and are ordered by ``$WIFIT3_RTL8821`` (DKMS default, ``=mainline``
falls back). See ``RTL8821AU_DKMS.md`` for the per-milestone ground truth.
"""
