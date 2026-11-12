"""rtlwifi cam.c: security CAM."""
from __future__ import annotations

from . import constants as C
from .constants import HalCfgMaps as maps
from .priv import RtlPriv


def rtl_cam_reset_all_entry(rtlpriv: RtlPriv) -> None:
    """cam.c:155: CAM_POLLINIG | clear-all."""
    ul_command = C.BIT(31) | C.BIT(30)
    rtlpriv.io.write32(maps.RWCAM, ul_command)


def rtl_cam_reset_sec_info(rtlpriv: RtlPriv) -> None:
    """cam.c:8. key_buf, key_len and pairwise_key belong to set_key (STA/AP), not kept here."""
    rtlpriv.sec.use_defaultkey = False
    rtlpriv.sec.pairwise_enc_algorithm = C.NO_ENCRYPTION
    rtlpriv.sec.group_enc_algorithm = C.NO_ENCRYPTION
