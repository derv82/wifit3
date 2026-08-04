"""RTL8822BU RX-side glue.

Reuses :mod:`wifit3.chips.rtw88_base.rx_common` for endpoint probing and
the 24-byte rx_pkt_desc decoder. Adds an 8822b-specific phy_status RSSI
parser (pages 0/1, mirrors `rtw8822b.c:query_phy_status`).
"""

from __future__ import annotations

import logging

from wifit3.chips.rtw88_base.rx_common import (  # noqa: F401  (re-exports)
    RX_PKT_DESC_SZ,
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)

logger = logging.getLogger(__name__)

# Kernel `min_rx_power` in rtw8822b.c query_phy_status_page0/1 — the
# lower clamp on signal_power before it reaches mac80211.
MIN_RX_POWER_DBM = -120

# Upper clamp on parser output. The chip's pwdb has a defined dynamic
# range of roughly 0-110 (mapping to -110..0 dBm); pwdb > 110 means
# "signal stronger than I can measure" — undefined behavior, not a real
# reading. Kernel rtw88 does NOT upper-clamp, so impossible positive
# dBm values flow through to userspace as-is. We DELIBERATELY deviate:
# clamp the parser output at 0 dBm because any real-world received
# power above 0 dBm (= 1 mW incident) requires the antenna to be in
# physical contact with the AP. This keeps EMA-smoothed `ap.signal`
# values bounded and prevents one close AP from polluting per-channel
# means via adjacent-channel leakage.
MAX_RX_POWER_DBM = 0

# Throttled warning when the chip saturates. Log spam is bounded to
# one message per N saturating frames.
_SATURATION_LOG_INTERVAL = 256
_saturation_count = 0


def _maybe_log_saturation(pwdb: int, path: str) -> None:
    global _saturation_count
    if pwdb <= 110:
        return
    _saturation_count += 1
    if _saturation_count % _SATURATION_LOG_INTERVAL == 1:
        logger.warning(
            "rtl8822bu phy_status %s pwdb=%d (raw would yield +%d dBm) — "
            "chip is saturating; clamped to %+d dBm. Hit %d times so far.",
            path, pwdb, pwdb - 110, MAX_RX_POWER_DBM, _saturation_count,
        )


def _clamp_dbm(dbm: int) -> int:
    """Bound output to physically plausible range [MIN, MAX]."""
    return max(min(dbm, MAX_RX_POWER_DBM), MIN_RX_POWER_DBM)


def parse_phy_status_rssi_8822b(
    buf: bytes, offset: int, _stat: RxPktStat
) -> int | None:
    """Mirror `rtw8822b.c:query_phy_status_page0` / `_page1` for RSSI.

    Returns approximate RSSI in dBm, clamped to ``[MIN_RX_POWER_DBM,
    MAX_RX_POWER_DBM] = [-120, 0]``. Lower bound matches the kernel's
    `signal_power = max3(rx_pwr_a, rx_pwr_b, -120)`. Upper bound is a
    deliberate deviation from kernel — see ``MAX_RX_POWER_DBM`` doc.

    Page 0 (CCK) → single PWDB at phy_status byte 1.
    Page 1 (OFDM/HT/VHT) → PWDB_A at byte 1, PWDB_B at byte 2; kernel
    takes max of both paths (8822b is 2T2R).

    Byte offsets confirmed against `GET_PHY_STAT_P0_PWDB` and
    `GET_PHY_STAT_P1_PWDB_A/B` macros (rtw8822b.h:124-132).
    """
    if len(buf) - offset < 4:
        return None
    page = buf[offset] & 0xF
    if page == 0:
        pwdb = buf[offset + 1]
        _maybe_log_saturation(pwdb, "page0")
        return _clamp_dbm(pwdb - 110)
    if page == 1:
        pwdb_a = buf[offset + 1]
        pwdb_b = buf[offset + 2]
        _maybe_log_saturation(max(pwdb_a, pwdb_b), "page1")
        return _clamp_dbm(max(pwdb_a, pwdb_b) - 110)
    return None


def iter_bulk_frames(buf: bytes):
    """Wrapper that hooks the 8822b phy_status RSSI parser into the
    shared iterator."""
    return _shared_iter_bulk_frames(
        buf, phy_status_rssi=parse_phy_status_rssi_8822b
    )
