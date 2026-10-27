"""rtlwifi base.c: RX config init and the 2 s watchdog work."""
from __future__ import annotations

from . import constants as C
from .priv import RtlPriv
from .wifi import is_hal_stop


def rtl_init_rx_config(rtlpriv: RtlPriv) -> None:
    """base.c:590."""
    rtlpriv.mac80211.rx_conf = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_RCR)


def rtl_watchdog_wq_callback(rtlpriv: RtlPriv) -> None:
    """base.c:2036, the steps a non-linked (monitor) interface runs. The linked traffic/LPS
    and roaming blocks are STA-only; the scan-list and ack-queue expiry touch no register."""
    if is_hal_stop(rtlpriv.rtlhal):
        return
    if not rtlpriv.cfg.mod_params.disable_watchdog:
        rtlpriv.cfg.ops.dm_watchdog(rtlpriv)
