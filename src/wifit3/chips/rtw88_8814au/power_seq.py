"""RTL8814AU power-on / power-off flows.

Loads the per-chip pwr_seq tables (extracted from kernel C by
`scripts/chips/rtw88_8814au/extract_pwr_seq.py`) and exposes the two top-level
flows that `mac.py:rtw_mac_power_switch` runs.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.power_seq import (
    CUT_ALL,
    INTF_USB,
    run_pwr_flow,
)

from .assets.pwr_seq import (
    CARD_DISABLE_FLOW_8814A,
    CARD_ENABLE_FLOW_8814A,
)
from .transport import RTL8814AUTransport


def card_enable_flow_8814a(transport: RTL8814AUTransport,
                           *, cut_mask: int = CUT_ALL) -> None:
    run_pwr_flow(transport, CARD_ENABLE_FLOW_8814A,
                 intf_mask=INTF_USB, cut_mask=cut_mask)


def card_disable_flow_8814a(transport: RTL8814AUTransport,
                            *, cut_mask: int = CUT_ALL) -> None:
    run_pwr_flow(transport, CARD_DISABLE_FLOW_8814A,
                 intf_mask=INTF_USB, cut_mask=cut_mask)
