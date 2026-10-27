"""rtlwifi cam.c: security CAM."""
from __future__ import annotations

from . import constants as C
from .constants import HalCfgMaps as maps
from .priv import RtlPriv


def rtl_cam_reset_all_entry(rtlpriv: RtlPriv) -> None:
    """cam.c:155: CAM_POLLINIG | clear-all."""
    ul_command = C.BIT(31) | C.BIT(30)
    rtlpriv.io.write32(maps.RWCAM, ul_command)
