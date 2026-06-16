"""RTL8822BU wifi-only coexistence HW config (the `rtw_btcoex_wifionly_hw_config` step).

The captured build runs the **wifi-only** coex path (`EEPROMBluetoothCoexist` is false on this dongle),
so there is no BT logic to port — just the static antenna/RFE seeding that hands the shared RF front-end
to WiFi: `ex_hal8822b_wifi_only_hw_config` `[SRC] hal/btc/halbtc8822bwifionly.c:19`. It forces the
antenna mux + gnt_wl=1 / gnt_bt=0 so the 2.4 GHz path is WiFi's. (The full BT-coex stack
`rtw_btcoex_HAL_Initialize` is NOT in this capture and is not ported.)
"""
from __future__ import annotations

from . import sipi


def wifi_only_hw_config(t) -> None:
    """[SRC] ex_hal8822b_wifi_only_hw_config — static wifi-only antenna/RFE seed (8 regs)."""
    sipi.set_bb_reg(t, 0x004C, 0x01800000, 0x2)   # BB control
    sipi.set_bb_reg(t, 0x0CB4, 0xFF, 0x77)        # SW control
    sipi.set_bb_reg(t, 0x0974, 0x300, 0x3)        # antenna mux switch
    sipi.set_bb_reg(t, 0x1990, 0x300, 0x0)
    sipi.set_bb_reg(t, 0x0CBC, 0x80000, 0x0)
    sipi.set_bb_reg(t, 0x0070, 0xFF000000, 0x0E)  # switch to WL-side controller + gnt debug
    sipi.set_bb_reg(t, 0x1704, 0xFFFFFFFF, 0x00007700)   # gnt_wl=1, gnt_bt=0 (plain 32-bit writes)
    sipi.set_bb_reg(t, 0x1700, 0xFFFFFFFF, 0xC00F0038)
