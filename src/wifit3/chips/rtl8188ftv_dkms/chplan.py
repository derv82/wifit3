"""RTL8188FTV DKMS channel-plan resolution (M2, pure).

Ported from ``hal_com_config_channel_plan`` (hal/hal_com.c:152-243) +
``rtw_get_chplan_from_country`` (core/rtw_rf.c:559-598) +
``rtw_chplan_is_empty`` (core/rtw_mlme_ext.c:307-318) with the
``RTW_ChannelPlanMap``/``country_chplan_map`` tables from regdomain.py
(machine-extracted verbatim). ``RTW_DEF_MODULE_REGULATORY_CERT`` is 0 and
neither ``CUSTOMIZED`` nor ``80211AC_VHT`` is set in this build, so the
module-map and def-module branches compile out.
"""
from __future__ import annotations

from . import regdomain

# include/rtw_mlme_ext.h:122-165
RTW_CHPLAN_WORLD_NULL = 0x20
RTW_CHPLAN_MAX = 0x61
RTW_CHPLAN_REALTEK_DEFINE = 0x7F

# hal/hal_com.c:130
EEPROM_CHANNEL_PLAN_BY_HW_MASK = 0x80

RTW_RD_2G_NULL = 0
RTW_RD_5G_NULL = 0


def _alpha2_specified(alpha2: bytes) -> bool:
    return int.from_bytes(alpha2[:2], "little") != 0xFFFF


def get_chplan_from_country(code: bytes) -> int | None:
    code = bytes([code[0], code[1]]).upper()
    for alpha2, chplan in regdomain.COUNTRY_CHPLAN:
        if alpha2.encode() == code:
            return chplan
    return None


def is_empty(plan: int) -> bool:
    if plan == RTW_CHPLAN_REALTEK_DEFINE:
        return True
    rd2g, rd5g, _lmt = regdomain.CHANNEL_PLAN_MAP[plan]
    return rd2g == RTW_RD_2G_NULL and rd5g == RTW_RD_5G_NULL


def is_valid(plan: int) -> bool:
    return (plan < RTW_CHPLAN_MAX or plan == RTW_CHPLAN_REALTEK_DEFINE) \
        and not is_empty(plan)


def config_channel_plan(hw_alpha2: bytes | None, hw_chplan: int,
                        sw_alpha2: bytes, sw_chplan: int,
                        def_chplan: int, autoload_fail: bool
                        ) -> tuple[int, bool, bytes | None]:
    force_hw = False
    chplan: int | None = None
    country: bytes | None = None
    if hw_chplan != 0xFF and not autoload_fail:
        if hw_chplan & EEPROM_CHANNEL_PLAN_BY_HW_MASK:
            force_hw = True
        hw_chplan &= ~EEPROM_CHANNEL_PLAN_BY_HW_MASK
    if not autoload_fail:
        if hw_alpha2 is not None and _alpha2_specified(hw_alpha2):
            ent = get_chplan_from_country(hw_alpha2)
            if ent is not None:
                chplan, country = ent, bytes(hw_alpha2[:2])
        if chplan is None and hw_chplan != 0xFF and is_valid(hw_chplan):
            chplan = hw_chplan
        elif chplan is None and force_hw and hw_chplan != 0xFF:
            force_hw = False
    if not force_hw:
        if _alpha2_specified(sw_alpha2):
            ent = get_chplan_from_country(sw_alpha2)
            if ent is not None:
                return ent, False, bytes(sw_alpha2[:2])
        if is_valid(sw_chplan):
            return sw_chplan, False, None
    if chplan is None:
        chplan = def_chplan
    return chplan, force_hw, country
