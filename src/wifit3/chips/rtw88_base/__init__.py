"""Shared rtw88-family infrastructure (cleanroom RE of `driver_sources/rtw88-source-v6.18/`).

Its only user, rtl8822cu, takes from it:

  * USB vendor control-transfer wire format for all register I/O
    (`bRequest=0x05`, `wValue=addr`, `wIndex=0`). See :mod:`transport`.
  * The `rtw_pwr_seq_cmd` table format (WRITE/POLLING/DELAY/END). See
    :mod:`power_seq`.
  * A common subset of MAC register addresses (REG_SYS_CFG1, REG_MCUFW_CTRL,
    REG_CR, ...). See :mod:`registers`.

Chip-specific bits (FW upload protocol, MAC power-on flow, BB/RF tables,
channel tuning lookups) stay in each chip's own package
(`chips.rtl8822cu.*`).
"""
