"""Shared rtw88-family infrastructure (cleanroom RE of `driver_sources/rtw88-source-v6.18/`).

Every rtw88 USB chip (8821a, 8812a, 8814a, 8822b, 8822c, 8723d, …) shares:

  * USB vendor control-transfer wire format for all register I/O
    (`bRequest=0x05`, `wValue=addr`, `wIndex=0`). See :mod:`transport`.
  * The `rtw_phy_cond` init-table format with the IF/ELIF/ELSE/ENDIF marker
    encoding. See :mod:`phy_cond`.
  * The `rtw_pwr_seq_cmd` table format (WRITE/POLLING/DELAY/END). See
    :mod:`power_seq`.
  * SIPI read/write to RF registers via REG_HSSI_READ + REG_LSSI_WRITE_*.
    See :mod:`rf_sipi`.
  * A common subset of MAC register addresses (REG_SYS_CFG1, REG_MCUFW_CTRL,
    REG_CR, ...). See :mod:`registers`.

Chip-specific bits (FW upload protocol, MAC power-on flow, BB/RF tables,
channel tuning lookups) stay in each chip's own package
(`chips.rtl8821au.*`, `chips.rtl8822bu.*`, ...).
"""
