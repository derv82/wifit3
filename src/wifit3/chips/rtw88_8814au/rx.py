"""RTL8814AU RX-side glue (M5) — monitor MAC init + frame iteration.

Reuses :mod:`wifit3.chips.rtw88_base.rx_common` for endpoint probing, the
24-byte rx_pkt_desc decode, and the burst frame iterator (the 8814a rx desc is
24 bytes, same as the 8822b). Per-frame RSSI comes from the jaguar phy_status
report (`parse_phy_status_rssi_8814a`, port of rtw8814a_query_phy_status).

`mac_init_for_rx` is the RX-relevant subset of rtw8814a_mac_init +
rtw_drv_info_cfg that was deferred from M2:
  - RXFLTMAP0/1/2     accept mgmt/ctrl/data subtypes
  - RX_DRVINFO_SZ     PHY_STATUS_SIZE (4) so phy_status rides each frame
  - rxdesc-len quirk  REG_TRXFF_BNDY+1 |= 0xF (mac.c:1378, 3081-only)
  - RCR               promiscuous monitor (RCR_MONITOR, incl. APP_PHYSTS)
  - WMAC_OPTION       clear bits 8|9 (rtw_drv_info_cfg)
  - USB burst         REG_RXDMA_MODE + REG_TXDMA_OFFSET_CHK drop-data

RX DMA itself is already enabled — REG_CR got MAC_TRX_ENABLE (0xFF, incl.
HCI_RXDMA/RXDMA/MACRXEN) back in M2's txdma_queue_mapping.
"""

from __future__ import annotations

import logging
import struct
import time

from wifit3.chips.rtw88_base.registers import DESC_RATE11M

from wifit3.chips.rtw88_base.rx_common import (  # noqa: F401 (re-exports)
    RX_PKT_DESC_SZ,
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)

from . import constants as C
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)


def mac_init_for_rx(transport: RTL8814AUTransport) -> None:
    """RX-relevant MAC init (deferred from M2): RX filters + drv_info + burst."""
    # RX filter maps (rtw8814a_mac_init).
    transport.write16(C.REG_RXFLTMAP0, C.RXFLTMAP0_8814A)
    transport.write16(C.REG_RXFLTMAP1, C.RXFLTMAP1_8814A)
    transport.write16(C.REG_RXFLTMAP2, C.RXFLTMAP2_8814A)

    # rtw_drv_info_cfg (mac.c:1373, 3081 path).
    transport.write8(C.REG_RX_DRVINFO_SZ, C.PHY_STATUS_SIZE)
    # "rxdesc len = 0" workaround: low nibble of REG_TRXFF_BNDY+1 = 0xF.
    v = (transport.read8(C.REG_TRXFF_BNDY + 1) & 0xF0) | 0x0F
    transport.write8(C.REG_TRXFF_BNDY + 1, v & 0xFF)
    # Promiscuous monitor RCR (includes APP_PHYSTS so phy_status rides frames).
    transport.write32(C.REG_RCR, C.RCR_MONITOR)
    transport.write32_clr(C.REG_WMAC_OPTION_FUNCTION + 4, (1 << 8) | (1 << 9))

    # USB RX burst (rtw_usb_init_burst_pkt_len) — HS uses BURST_SIZE_512.
    BIT_DMA_MODE = 1 << 1
    BIT_DMA_BURST_CNT = (1 << 2) | (1 << 3)
    BIT_DMA_BURST_SIZE_512 = 1
    rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE
    rxdma |= (BIT_DMA_BURST_SIZE_512 << 4) & 0x30
    transport.write8(C.REG_RXDMA_MODE, rxdma & 0xFF)
    BIT_DROP_DATA_EN = 1 << 9
    transport.write16(C.REG_TXDMA_OFFSET_CHK,
                      transport.read16(C.REG_TXDMA_OFFSET_CHK) | BIT_DROP_DATA_EN)

    configure_rx_aggregation(transport)
    tune_monitor_cck_sensitivity(transport)


def configure_rx_aggregation(transport: RTL8814AUTransport) -> None:
    """Match the kernel's monitor/unassociated RX-aggregation state: OFF
    (size=0, timeout=1 — the `rtw_usb_dynamic_rx_agg_v1` disable path), so the
    RX-DMA flushes each frame to bulk-IN immediately instead of accumulating
    pages. [WIRE] cold-boot pcap writes REG_RXDMA_AGG_PG_TH=0x0100 throughout
    monitor. Aggregation (size=5) can leave the RX-DMA page accumulator un-armed
    so delivery stops; framing is unaffected (each transfer still starts on an
    rx_pkt_desc). See RTL8814AU.md.
    """
    transport.write8_set(C.REG_TXDMA_PQ_MAP, C.BIT_RXDMA_AGG_EN)
    transport.write8_clr(C.REG_RXDMA_AGG_PG_TH + 3, 1 << 7)
    val16 = (C.RXDMA_AGG_SIZE & 0xFF) | ((C.RXDMA_AGG_TIMEOUT & 0xFF) << 8)
    transport.write16(C.REG_RXDMA_AGG_PG_TH, val16)
    logger.debug("RX aggregation: kernel monitor config (off, size=0x%02x "
                "timeout=0x%02x, immediate flush)",
                C.RXDMA_AGG_SIZE, C.RXDMA_AGG_TIMEOUT)


def apply_monitor_rcr(transport: RTL8814AUTransport) -> None:
    """Force the promiscuous monitor RCR (also re-applied on warm reattach)."""
    transport.write32(C.REG_RCR, C.RCR_MONITOR)
    rcr = transport.read32(C.REG_RCR)
    logger.debug("RX filter: RCR=0x%08x (AAP=%d)", rcr, 1 if rcr & 0x1 else 0)


# ACK is control subtype 13; bit N of RXFLTMAP1 gates control subtype N. mac_init_for_rx
# leaves RXFLTMAP1=RXFLTMAP1_8814A (0x0400, bit13 clear), so the AP's ACKs to our injects
# are dropped by default; TX-ACK detection opens only bit13.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(transport: RTL8814AUTransport) -> None:
    """RXFLTMAP1 |= BIT(13): let RX see the AP's ACKs to our injects. Off by default."""
    transport.write16(C.REG_RXFLTMAP1,
                      transport.read16(C.REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(transport: RTL8814AUTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter (RXFLTMAP1_8814A)."""
    transport.write16(C.REG_RXFLTMAP1,
                      transport.read16(C.REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def tune_monitor_cck_sensitivity(transport: RTL8814AUTransport) -> None:
    """Maximise CCK RX sensitivity for monitor (beacons are 1 Mbps CCK).

    Ports rtw8814a_config_cck_rx_antenna_init (2R CCA + MRC + RX diversity) and
    forces the CCK packet-detect threshold to the most-sensitive level (LV0).
    The kernel drives REG_CCK_PD_TH from a dynamic watchdog (cck_pd_set) keyed
    on association/RSSI; in always-monitor we don't run it, so we pin LV0 here.
    [[feedback_monitor_mode_deviation]]
    """
    # config_cck_rx_antenna_init
    transport.write32_clr(C.REG_RXSB_CCK, C.BIT_RXSB_ANA_DIV)   # disable ant-div
    transport.write32_clr(C.REG_CCA, C.BIT_CCA_CO)              # concurrent CCA
    transport.write32_clr(C.REG_ANTSEL, C.BIT_ANT_BYCO)        # RX path diversity
    transport.write32_clr(C.REG_PRECTRL, C.BIT_DIS_CO_PATHSEL)  # en MRC antsel
    transport.write32_mask(C.REG_CCA_MF, C.BIT_MBC_WIN, 1)      # MBC weighting
    transport.write32_set(C.REG_CCKTX, C.BIT_CMB_CCA_2R)        # 2R CCA only
    # Most-sensitive CCK packet-detect threshold (LV0).
    transport.write8(C.REG_CCK_PD_TH, C.CCK_PD_TH_MAX_SENS)
    logger.debug("CCK RX sensitivity: 2R-CCA+MRC on, CCK_PD_TH=0x%02x (LV0/max)",
                C.CCK_PD_TH_MAX_SENS)


def reset_phy_counters(transport: RTL8814AUTransport) -> None:
    """Reset FA/CCA/CRC counters (tail of rtw8814a_false_alarm_statistics)."""
    transport.write32_set(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_FAS, 1 << 17)
    transport.write32_clr(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CCK0_FAREPORT, 1 << 15)
    transport.write32_set(C.REG_CNTRST, 1 << 0)
    transport.write32_clr(C.REG_CNTRST, 1 << 0)


def read_crc_ok(transport: RTL8814AUTransport) -> int:
    """BB CRC-OK count (cck+ofdm+ht) since the last counter reset — read-only.

    Same baseband counters as rf_receiving_frames, but without the reset, so it
    can be sampled each watchdog tick BEFORE read_total_fa_cnt clears them: the
    value is then "frames the BB demodulated in this window". Sampling it next
    to the RX-DMA state answers whether a wedged boot has stopped decoding (BB
    dead) or is still decoding but dropping post-filter (CRC-ok climbs, FIFO
    stays empty)."""
    cck_ok = transport.read32(C.REG_CRC_CCK) & 0xFFFF
    ofdm_ok = transport.read32(C.REG_CRC_OFDM) & 0xFFFF
    ht_ok = transport.read32(C.REG_CRC_HT) & 0xFFFF
    return cck_ok + ofdm_ok + ht_ok


def rf_receiving_frames(transport: RTL8814AUTransport,
                        settle_s: float = 2.0, poll_s: float = 0.1) -> bool:
    """True if the PHY actually DEMODULATES frames within `settle_s`.

    Gates on CRC-OK counts (frames that passed FCS), not just CCA energy —
    a cold-boot chip has THREE states: RF-deaf (CCA=0), demod-fail (CCA>0 but
    CRC-OK=0, delivers garbage), and good (CRC-OK>0). Only CRC-OK>0 confirms a
    usable boot; CCA alone would pass a demod-fail boot. Counters accumulate in
    BB hardware passively. See RTL8814AU.md.

    Polls every `poll_s` and returns the instant a frame demodulates, so a
    healthy boot clears in ~one beacon interval instead of always blocking the
    full `settle_s`; only a deaf boot waits the whole window before failing.
    """
    reset_phy_counters(transport)
    deadline = time.monotonic() + settle_s
    cck_ok = ofdm_ok = ht_ok = 0
    while True:
        time.sleep(poll_s)
        cck_ok = transport.read32(C.REG_CRC_CCK) & 0xFFFF
        ofdm_ok = transport.read32(C.REG_CRC_OFDM) & 0xFFFF
        ht_ok = transport.read32(C.REG_CRC_HT) & 0xFFFF
        if (cck_ok + ofdm_ok + ht_ok) > 0 or time.monotonic() >= deadline:
            break
    cca_ofdm = (transport.read32(C.REG_CCA_OFDM) >> 16) & 0xFFFF
    demod_ok = cck_ok + ofdm_ok + ht_ok
    logger.debug("RF probe: CRC-ok=%d (cck=%d ofdm=%d ht=%d) cca=%d -> %s",
                demod_ok, cck_ok, ofdm_ok, ht_ok, cca_ofdm,
                "RECEIVING" if demod_ok else ("demod-fail" if cca_ofdm else "RF-deaf"))
    return demod_ok > 0


# --- RSSI from the jaguar phy_status report (rtw8814a_query_phy_status) -------
_RSSI_MIN, _RSSI_MAX = -110, 0


def _cck_rx_pwr(lna_idx: int, vga_idx: int) -> int:
    """rtw8814a_cck_rx_pwr — CCK signal power (dBm) from AGC LNA/VGA indices."""
    return {7: -38, 5: -28, 3: -8, 2: -1}.get(lna_idx, 0) - 2 * vga_idx


def parse_phy_status_rssi_8814a(buf: bytes, offset: int, stat) -> int | None:
    """Signal power (dBm) from the 28-byte jaguar phy_status report.

    Mirrors rtw8814a_query_phy_status (rtw88xxa.h jaguar layout). CCK uses the
    AGC LNA/VGA lookup; OFDM uses per-path gain-110, taking the 2nd-lowest of
    the 4 paths (kernel's power-save robustness trick).
    """
    if len(buf) - offset < 28:
        return None
    w0, w1 = struct.unpack_from("<II", buf, offset)
    w5, w6 = struct.unpack_from("<II", buf, offset + 20)

    if stat.rate <= DESC_RATE11M:                      # CCK
        vga = (w1 >> 8) & 0x1F
        lna = (w1 >> 13) & 0x07
        sig = _cck_rx_pwr(lna, vga)
    else:                                              # OFDM/HT/VHT
        g_a = w0 & 0x7F
        g_b = (w0 >> 8) & 0x7F
        g_c = (w5 >> 24) & 0x7F
        g_d = w6 & 0x7F
        middle1 = max(min(g_a, g_b), min(g_c, g_d))
        middle2 = min(max(g_a, g_b), max(g_c, g_d))
        sig = min(middle1, middle2) - 110
    return max(_RSSI_MIN, min(_RSSI_MAX, sig))


def iter_bulk_frames(buf: bytes):
    """Yield (stat, mpdu, rssi) per frame, with real per-frame RSSI (dBm)."""
    return _shared_iter_bulk_frames(buf, phy_status_rssi=parse_phy_status_rssi_8814a)
