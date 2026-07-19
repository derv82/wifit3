"""RTL8821CU USB TX-descriptor builder — the full ``update_txdesc`` management-frame filler.

[SRC] hal/rtl8821c/usb/rtl8821cu_xmit.c:35 ``update_txdesc``, the USB filler reached from
``dump_mgntframe`` -> ``rtw_dump_xframe`` for management / reserved-page frames. Field bit
positions are the HALMAC NIC tx-desc macros [SRC] hal/halmac/halmac_tx_desc_nic.h; the 16-bit
checksum is the XOR of the first 32 bytes taken as LE halfwords [SRC]
hal/halmac/halmac_88xx/halmac_8821c/halmac_common_8821c.c:139 ``fill_txdesc_check_sum_8821c``.

Why this is separate from firmware.py's minimal descriptor: the cold-init FW download set
``not_xmitframe_fw_dl=1`` ([SRC] hal_com.c:1578) so its reserved-page write took the minimal
``usb_write_data_not_xmitframe`` path; the real ``_halmac_init_hal`` (airmon monitor entry)
leaves the flag 0, so the same FW chunks are written through ``dump_mgntframe`` -> this builder
(the only byte-difference between the two FW downloads). The same descriptor fills management TX
(deauth/inject), so this carries the whole MGNT_FRAMETAG branch, not just the rsvd-page subset.

The defaults mirror ``update_mgntframe_attrib`` [SRC] core/rtw_mlme_ext.c:7707: an early-init
adapter has tx_rate = 1M (CCK), so raid = RATEID_IDX_B and the HW data-rate is 0.
"""
from __future__ import annotations

TXDESC_SIZE = 48                # [SRC] rtw_xmit.h:219 TXDESC_SIZE (HALMAC_TX_DESC_SIZE_8821C)
_OFFSET_SZ = 0                  # [SRC] rtl8821cu.h:45 OFFSET_SZ (USB)

RTW_DEFAULT_MGMT_MACID = 1      # [SRC] drv_types.h:886
RATEID_IDX_B = 8                # [SRC] ieee80211.h:248 — raid for WIRELESS_11B on HALMAC ICs
_HWRATE_1M = 0                  # MRateToHwRate(MGN_1M): DESC_RATE1M

_QSEL_BEACON = 0x10             # [SRC] halmac_type.h HALMAC_TXDESC_QSEL_BEACON
RETRY_COUNT = 6                 # [SRC] hal/rtl8821c/rtl8821c_ops.c:3217


def _set_field(buf: bytearray, off: int, start: int, length: int, value: int) -> None:
    """SET_BITS_TO_LE_4BYTE — set bits [start, start+length) of the LE u32 at buf[off]."""
    word = int.from_bytes(buf[off:off + 4], "little")
    mask = ((1 << length) - 1) << start
    word = (word & ~mask) | ((value << start) & mask)
    buf[off:off + 4] = (word & 0xFFFFFFFF).to_bytes(4, "little")


def fill_checksum(buf: bytearray) -> None:
    """fill_txdesc_check_sum_8821c: XOR the first 32 B as 16 LE halfwords into TXDESC_CHECKSUM
    (the field is zeroed before the XOR so the result is independent of its prior value)."""
    _set_field(buf, 0x1C, 0, 16, 0)
    chksum = 0
    for i in range(16):
        chksum ^= int.from_bytes(buf[2 * i:2 * i + 2], "little")
    _set_field(buf, 0x1C, 0, 16, chksum & 0xFFFF)


def build_mgnt_txdesc(payload: bytes, *, qsel: int, rate_hw: int = _HWRATE_1M,
                      mac_id: int = RTW_DEFAULT_MGMT_MACID, raid: int = RATEID_IDX_B,
                      retry_ctrl: bool = True, mbssid: int = 0, hw_port: int = 0,
                      hw_ssn_sel: int = 0, qos_en: bool = False, seqnum: int = 0) -> bytes:
    """``update_txdesc`` MGNT_FRAMETAG branch: build [48-byte desc][payload] with the HW XOR
    checksum. ``qsel`` is the caller's queue (BEACON for a reserved-page FW chunk, MGNT for a
    deauth/inject frame). ``retry_ctrl`` enables RTS_DATA_RTY_LMT (True → RETRY_COUNT retries,
    False → single-shot). The NDPA/beamformer sub-branch and the DATA_FRAMETAG branch are
    not ported (no path this driver drives takes them: FW reserved-page download, deauth).

    BMC follows the kernel: ``rtw_hal_mgnt_xmit`` runs ``update_mgntframe_attrib_addr`` [SRC]
    hal_intf.c:885 / rtw_mlme_ext.c:7794 first, copying ``ra = addr1`` (the frame's bytes 4-9)
    into the attrib, so ``bmcst = IS_MCAST(ra) = payload[4] & 1`` — true here even for FW chunks,
    whose byte 4 lands in the addr1 slot."""
    size = len(payload)
    buf = bytearray(TXDESC_SIZE + size)
    buf[TXDESC_SIZE:] = payload

    _set_field(buf, 0x00, 26, 1, 1)                     # LS (USB only)
    _set_field(buf, 0x00, 0, 16, size)                  # TXPKTSIZE
    _set_field(buf, 0x00, 16, 8, TXDESC_SIZE + _OFFSET_SZ)  # OFFSET
    if payload[4] & 0x01:
        _set_field(buf, 0x00, 24, 1, 1)                 # BMC = IS_MCAST(addr1)

    _set_field(buf, 0x14, 21, 3, hw_port)               # PORT_ID
    _set_field(buf, 0x14, 18, 3, hw_port)               # MULTIPLE_PORT

    _set_field(buf, 0x04, 0, 7, mac_id)                 # MACID
    _set_field(buf, 0x04, 16, 5, raid)                  # RATE_ID
    _set_field(buf, 0x04, 8, 5, qsel)                   # QSEL

    if not qos_en:
        _set_field(buf, 0x00, 31, 1, 1)                 # DISQSELSEQ
        _set_field(buf, 0x20, 15, 1, 1)                 # EN_HWSEQ
        _set_field(buf, 0x0C, 6, 2, hw_ssn_sel)         # HW_SSN_SEL
        _set_field(buf, 0x20, 14, 1, 0)                 # EN_HWEXSEQ
    else:
        _set_field(buf, 0x24, 12, 12, seqnum)           # SW_SEQ

    # MGNT_FRAMETAG branch (non-NDPA): fixed-rate mgmt frame.
    _set_field(buf, 0x18, 12, 4, mbssid & 0xF)          # MBSSID
    _set_field(buf, 0x0C, 8, 1, 1)                      # USE_RATE
    _set_field(buf, 0x10, 0, 7, rate_hw)                # DATARATE
    _set_field(buf, 0x10, 17, 1, 1)                     # RTY_LMT_EN
    _set_field(buf, 0x10, 18, 6, RETRY_COUNT if retry_ctrl else 0)  # RTS_DATA_RTY_LMT [SRC] rtl8821c_ops.c:3216

    _set_field(buf, 0x18, 0, 12, 0x01)                  # SW_DEFINE (DriverFixedRate)

    fill_checksum(buf)
    return bytes(buf)
