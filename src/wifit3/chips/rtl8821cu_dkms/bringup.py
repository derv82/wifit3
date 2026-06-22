"""RTL8821CU cold bring-up sequence — the canonical init the byte-for-byte gate verifies.

The cold path, in the order the wire shows:
  1. halmac mount chip-detect  (SYS_CFG2 / SYS_CFG1+1)         — chipid.mount_get_chip_info
  2. chip-version read         (SYS_CFG1 / SYS_STATUS1 / 0x68) — chipid.read_chip_version
  3. EFUSE dump + decode + parse                                — efuse.read_efuse
  4. MAC-hidden-rpt readback   (power-on + FW download + C2H)   — read_mac_hidden_rpt
The chip-info read (`rtl8821c_read_efuse`) ends by calling `hal_read_mac_hidden_rpt`, which is
what actually powers the chip and downloads firmware on this driver — so the cold FW download
lives here, not in the later `_halmac_init_hal`. Later milestones (MAC/BB/RF init, monitor
entry) extend ``cold_bringup`` past the report readback.
"""
from __future__ import annotations

from . import btc, chipid, efuse, firmware, init, pwrseq

REG_C2HEVT_MSG_NORMAL = 0x01A0      # [SRC] include/hal_com_reg.h:149
_C2H_DEFEATURE_RSVD = 0xFD          # [SRC] hal/hal_com_c2h.h:79 — "FW: report MAC-hidden via reg"
_C2H_DBG = 0x00                     # [SRC] hal/hal_com_c2h.h:51 — "host done reading"


def power_on(t, info, already_on: bool = False) -> bool:
    """[SRC] rtl8821c_power_on halinit.c:78 + rtw_hal_power_on btc tail hal_intf.c:461.

    The APFM_ON_MAC software flag makes the power sequence idempotent: the first call runs
    hal_power_on (pre-init, card-enable switch, post-switch config) and the second skips it,
    but the BT-coex power-on setting re-runs either way (combo card). Returns the new flag.
    """
    if not already_on:
        init.pre_init_system_cfg(t)
        pwrseq.mac_pwr_switch(t, power_on=True)
        init.init_system_cfg(t)
    if info.bt_coexist:
        btc.power_on_setting(t, info.rfe_type, info.single_ant_path)
    return True


def read_mac_hidden_rpt(t, info) -> None:
    """[SRC] hal_read_mac_hidden_rpt hal_com.c:1550 (USB path).

    Power the chip on, tell the FW to report its MAC-hidden capability bits through a register
    (C2HEVT = DEFEATURE), download firmware (which powers on again — idempotent), then poll the
    report and acknowledge. The cold-boot firmware download is reached from here.
    """
    on = power_on(t, info, already_on=False)
    t.write8(REG_C2HEVT_MSG_NORMAL, _C2H_DEFEATURE_RSVD)
    firmware.fw_dl(t, info, on, power_on)


def cold_bringup(t) -> None:
    """The cold init the driver's connect() runs, in the order the wire shows. See module
    docstring. Verified byte-for-byte by ``scripts/verify_pcap.py rtl8821cu_dkms``."""
    chipid.mount_get_chip_info(t)
    chipid.read_chip_version(t)
    info = efuse.read_efuse(t)
    read_mac_hidden_rpt(t, info)
