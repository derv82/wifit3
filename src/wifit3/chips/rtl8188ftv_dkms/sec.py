"""RTL8188FTV DKMS security/CAM (M5h).

``invalidate_cam_all`` ported from ``rtw_hal_set_hwreg(HW_VAR_CAM_INVALID_ALL)``
(hal/rtl8188f/rtl8188f_hal_init.c:6244-6247, ``RWCAM = REG_CAMCMD``):
a single ``BIT31|BIT30`` write. The sw CAM-cache clear has no wire.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


def invalidate_cam_all(t) -> None:
    t.write32(0x0670, BIT(31) | BIT(30))
