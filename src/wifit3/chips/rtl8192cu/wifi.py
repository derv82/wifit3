"""wifi.h static inline helpers: cfg->ops register dispatch and HAL state."""
from __future__ import annotations

from . import constants as C
from .priv import RtlHal, RtlPriv


def rtl_get_bbreg(rtlpriv: RtlPriv, regaddr: int, bitmask: int) -> int:
    return rtlpriv.cfg.ops.get_bbreg(rtlpriv, regaddr, bitmask)


def rtl_set_bbreg(rtlpriv: RtlPriv, regaddr: int, bitmask: int, data: int) -> None:
    rtlpriv.cfg.ops.set_bbreg(rtlpriv, regaddr, bitmask, data)


def rtl_get_rfreg(rtlpriv: RtlPriv, rfpath: int, regaddr: int, bitmask: int) -> int:
    return rtlpriv.cfg.ops.get_rfreg(rtlpriv, rfpath, regaddr, bitmask)


def rtl_set_rfreg(rtlpriv: RtlPriv, rfpath: int, regaddr: int, bitmask: int, data: int) -> None:
    rtlpriv.cfg.ops.set_rfreg(rtlpriv, rfpath, regaddr, bitmask, data)


def calculate_bit_shift(bitmask: int) -> int:
    """__ffs(bitmask); 0 for an empty mask (the WARN_ON_ONCE path)."""
    if not bitmask:
        return 0
    return (bitmask & -bitmask).bit_length() - 1


def is_hal_stop(rtlhal: RtlHal) -> bool:
    return rtlhal.state == C._HAL_STATE_STOP


def set_hal_start(rtlhal: RtlHal) -> None:
    rtlhal.state = C._HAL_STATE_START


def set_hal_stop(rtlhal: RtlHal) -> None:
    rtlhal.state = C._HAL_STATE_STOP
