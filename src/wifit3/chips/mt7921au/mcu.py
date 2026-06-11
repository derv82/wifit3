"""
MT7921AU connac2 MCU command layer.

A faithful port of two kernel pieces:

  * the command-id field encoding — the ``MCU_CMD`` / ``MCU_EXT_CMD`` /
    ``MCU_UNI_CMD`` / ``MCU_CE_CMD`` macros in mt76_connac_mcu.h. A command is a
    single 32-bit int packing the id, ext-id, and UNI/CE/QUERY/WA flag bits;
  * ``build_mcu_frame`` — mt76_connac2_mcu_fill_message (mt76_connac_mcu.c) plus
    the USB-specific SDIO header + tail pad (mt7921u_mcu_send_message). It turns
    an encoded command + payload into the exact on-wire bytes.

The per-command payload builders below mirror their kernel counterparts 1:1; each
cites the source function. Values come from constants.py (grepped from regs.h),
never typed from memory.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *

# ---------------------------------------------------------------------------
# Command-id field encoding (mt76_connac_mcu.h __MCU_CMD_FIELD_*).
# ---------------------------------------------------------------------------
_F_ID = 0x000000FF      # GENMASK(7, 0)   command id
_F_EXTID = 0x0000FF00   # GENMASK(15, 8)  ext command id
_F_QUERY = 1 << 16      # BIT(16)         query (vs set)
_F_UNI = 1 << 17        # BIT(17)         unified command (uni_txd)
_F_CE = 1 << 18         # BIT(18)         offload (CE) command
_F_WA = 1 << 19         # BIT(19)         destined for WA (vs WM)

MCU_CMD_EXT_CID = 0xED  # the cid byte carried by every EXT command


def MCU_CMD(t):
    return t & _F_ID


def MCU_EXT_CMD(t):
    return MCU_CMD_EXT_CID | ((t << 8) & _F_EXTID)


def MCU_EXT_QUERY(t):
    return MCU_EXT_CMD(t) | _F_QUERY


def MCU_UNI_CMD(t):
    return _F_UNI | (t & _F_ID)


def MCU_CE_CMD(t):
    return _F_CE | (t & _F_ID)


def MCU_CE_QUERY(t):
    return MCU_CE_CMD(t) | _F_QUERY


# Command ids (the enums in mt76_connac_mcu.h). Only the ones this driver emits.
# EXT (MCU_EXT_CMD_*)
EXT_CMD_EFUSE_BUFFER_MODE = 0x21
EXT_CMD_PROTECT_CTRL = 0x3E
EXT_CMD_MAC_INIT_CTRL = 0x46
EXT_CMD_SET_RX_PATH = 0x4E
EXT_CMD_CHANNEL_SWITCH = 0x08
# CE (MCU_CE_CMD_*)
CE_CMD_SET_RX_FILTER = 0x0A
CE_CMD_SET_CHAN_DOMAIN = 0x0F
CE_CMD_SET_BSS_ABORT = 0x17
CE_CMD_SET_CLC = 0x5C
CE_CMD_SET_RATE_TX_POWER = 0x5D
CE_CMD_GET_NIC_CAPAB = 0x8A
CE_CMD_CHIP_CONFIG = 0xCA
CE_CMD_FWLOG_2_HOST = 0xC5
# UNI (MCU_UNI_CMD_*)
UNI_CMD_DEV_INFO_UPDATE = 0x01
UNI_CMD_BSS_INFO_UPDATE = 0x02
UNI_CMD_SNIFFER = 0x24

# uni_txd.option = MCU_CMD_UNI_EXT_ACK = ACK | UNI | SET (BIT0|BIT1|BIT2).
MCU_CMD_UNI_EXT_ACK = 0x07

UNI_TXD_SIZE = 48   # sizeof(struct mt76_connac2_mcu_uni_txd)
# MCU_TXD_SIZE (64) comes from constants.


def build_mcu_frame(cmd, payload, seq):
    """Port of mt76_connac2_mcu_fill_message + the USB SDIO header and tail pad.

    Returns the on-wire bytes for ``cmd`` (an encoded command int) carrying
    ``payload``, stamped with the 4-bit ``seq``. Both txd shapes begin with
    ``__le32 txd[8]`` (32 B), so ``len`` is uniformly ``skb_len - 32``.

    Wire layout: ``[4B SDIO hdr][txd: 64B std | 48B uni][payload][pad]``.
    """
    mcu_cmd = cmd & _F_ID
    uni = bool(cmd & _F_UNI)
    txd_size = UNI_TXD_SIZE if uni else MCU_TXD_SIZE
    skb_len = txd_size + len(payload)            # skb->len after skb_push(txd)

    t = SDIO_HDR_SIZE                            # txd starts after the SDIO hdr
    frame = bytearray(t + txd_size + len(payload))

    # 4-byte SDIO header prepended by mt792x_skb_add_usb_sdio_hdr: tx_bytes = skb->len.
    struct.pack_into("<I", frame, 0, skb_len & 0xFFFF)

    # txd[0]/txd[1] — identical for both shapes.
    struct.pack_into("<I", frame, t + 0, TXD0_BASE | (skb_len & 0xFFFF))
    struct.pack_into("<I", frame, t + 4, TXD1_CMD)
    struct.pack_into("<H", frame, t + 32, (skb_len - 32) & 0xFFFF)   # ->len

    if uni:
        struct.pack_into("<H", frame, t + 34, mcu_cmd)   # cid (le16)
        frame[t + 37] = MCU_PKT_ID                       # pkt_type
        frame[t + 39] = seq & 0xFF
        frame[t + 42] = MCU_S2D_H2N                      # s2d_index
        frame[t + 43] = MCU_CMD_UNI_EXT_ACK              # option
    else:
        struct.pack_into("<H", frame, t + 34, MCU_PQ_ID)  # pq_id = 0x8000
        frame[t + 36] = mcu_cmd                           # cid
        frame[t + 37] = MCU_PKT_ID                        # pkt_type
        ext_cid = (cmd & _F_EXTID) >> 8
        if ext_cid or (cmd & _F_CE):
            set_query = MCU_Q_QUERY if (cmd & _F_QUERY) else MCU_Q_SET
        else:
            set_query = MCU_Q_NA
        frame[t + 38] = set_query
        frame[t + 39] = seq & 0xFF
        frame[t + 41] = ext_cid
        frame[t + 42] = MCU_S2D_H2N                       # H2C only for WA cmds
        frame[t + 43] = 1 if ext_cid else 0               # ext_cid_ack

    frame[t + txd_size:] = payload

    # Tail pad: round_up(len, 4) + 4 (mt7921u_mcu_send_message).
    pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
    frame.extend(b"\x00" * pad)
    return bytes(frame)


# ===========================================================================
# Per-command payload builders. Each returns (cmd, payload) and cites its
# kernel source. The transport stamps the seq and frames it via build_mcu_frame.
# ===========================================================================

def get_nic_capability():
    """mt7921_mcu_get_nic_capability — MCU_CE_CMD(GET_NIC_CAPAB), empty body.
    Sent via send_and_get_msg (waits for the reply) but encoded as a plain CE
    SET command, not a QUERY. The reply carries MAC address + PHY/chip caps."""
    return MCU_CE_CMD(CE_CMD_GET_NIC_CAPAB), b""


def fw_log_2_host(ctrl):
    """mt7921_mcu_fw_log_2_host — MCU_CE_CMD(FWLOG_2_HOST). { u8 ctrl_val; u8 pad[3]; }."""
    return MCU_CE_CMD(CE_CMD_FWLOG_2_HOST), struct.pack("<B3x", ctrl)


def set_eeprom():
    """mt7921_mcu_set_eeprom — MCU_EXT_CMD(EFUSE_BUFFER_MODE).
    req_hdr { u8 buffer_mode=EE_MODE_EFUSE; u8 format=EE_FORMAT_WHOLE; __le16 len=0; }."""
    return MCU_EXT_CMD(EXT_CMD_EFUSE_BUFFER_MODE), struct.pack(
        "<BBH", EE_MODE_EFUSE, EE_FORMAT_WHOLE, 0)


def set_rts_thresh(val, band):
    """mt76_connac_mcu_set_rts_thresh — MCU_EXT_CMD(PROTECT_CTRL).
    { u8 prot_idx=1; u8 band; u8 rsv[2]; __le32 len_thresh=val; __le32 pkt_thresh=2; }."""
    return MCU_EXT_CMD(EXT_CMD_PROTECT_CTRL), struct.pack(
        "<BB2xII", 1, band, val, 0x2)


def set_channel_domain():
    """mt76_connac_mcu_set_channel_domain — MCU_CE_CMD(SET_CHAN_DOMAIN), no reply.

    [ hdr ][ per-channel { __le16 hw_value; __le16 pad; __le32 flags; } ]. We
    announce the world ('00') domain (regdomain.py); the kernel skips DISABLED
    channels, so the body is just the enabled 2.4/5 GHz channels with their
    cfg80211 flags. hdr: alpha2[4], bw_2g, bw_5g, bw_6g, pad, n_2ch, n_5ch,
    n_6ch, pad2."""
    from . import regdomain as rd
    ch = rd.CHANNELS_2GHZ + rd.CHANNELS_5GHZ
    hdr = struct.pack("<4sBBBBBBBB", rd.WORLD_ALPHA2, rd.WORLD_BW_2G, rd.WORLD_BW_5G,
                      rd.WORLD_BW_6G, 0, len(rd.CHANNELS_2GHZ), len(rd.CHANNELS_5GHZ), 0, 0)
    body = b"".join(struct.pack("<HHI", hw, 0, flags) for hw, flags in ch)
    return MCU_CE_CMD(CE_CMD_SET_CHAN_DOMAIN), hdr + body


def set_rate_txpower(payload):
    """One SET_RATE_TX_POWER batch (mt76_connac_mcu_skb_send_msg, no reply). The
    per-batch payloads are built by txpower.rate_txpower_payloads()."""
    return MCU_CE_CMD(CE_CMD_SET_RATE_TX_POWER), payload


def set_mac_enable(band, enable):
    """mt76_connac_mcu_set_mac_enable — MCU_EXT_CMD(MAC_INIT_CTRL).
    { u8 enable; u8 band; u8 rsv[2]; }."""
    return MCU_EXT_CMD(EXT_CMD_MAC_INIT_CTRL), struct.pack("<BB2x", 1 if enable else 0, band)


# CH_SWITCH reasons (mt76_connac_mcu.h). SET_RX_PATH and monitor mode use NORMAL.
CH_SWITCH_NORMAL = 0
# mt7921 is 2x2: phy->mt76->antenna_mask = chainmask = 0x3.
ANTENNA_MASK = 0x3
# Default chandef at __mt7921_start, before any channel is set (observed on the
# wire in the start-time SET_RX_PATH; deterministic across units/captures):
# 6 GHz channel 1, 20 MHz. channel_band 2 = 6 GHz (the firmware's band code).
DEFAULT_CHANDEF = {"control_ch": 1, "center_ch": 1, "bw": 0, "channel_band": 2, "band_idx": 0}


def set_chan_info(ext_cmd, chandef, antenna_mask=ANTENNA_MASK):
    """mt7921_mcu_set_chan_info — MCU_EXT_CMD(ext_cmd), used with SET_RX_PATH (at
    radio start) or CHANNEL_SWITCH. 76-byte req describing channel + streams."""
    tx_streams = bin(antenna_mask).count("1")          # hweight8(antenna_mask)
    rx_streams = antenna_mask
    if ext_cmd == EXT_CMD_CHANNEL_SWITCH:
        rx_streams = bin(rx_streams).count("1")
    req = struct.pack(
        "<BBBBBBBBHBBIBBB57x",
        chandef["control_ch"], chandef["center_ch"], chandef["bw"],
        tx_streams, rx_streams, CH_SWITCH_NORMAL, chandef["band_idx"], 0,  # center_ch2
        0,                                              # cac_case
        chandef["channel_band"], 0,                     # channel_band, rsv0
        0,                                              # outband_freq
        0, 0, 0,                                        # txpower_drop, ap_bw, ap_center_ch
    )
    return MCU_EXT_CMD(ext_cmd), req


def set_deep_sleep(enable):
    """mt76_connac_mcu_set_deep_sleep — MCU_CE_CMD(CHIP_CONFIG), no reply.

    struct mt76_connac_config { __le16 id; u8 type; u8 resp_type; __le16 data_size;
    __le16 resv; u8 data[320]; } with data = snprintf("KeepFullPwr %d", !enable).
    USB leaves pm.ds_enable = 0, so init sends enable=False -> "KeepFullPwr 1"."""
    data = (b"KeepFullPwr %d" % (0 if enable else 1)).ljust(320, b"\x00")
    payload = struct.pack("<HBBHH", 0, 0, 0, 0, 0) + data
    return MCU_CE_CMD(CE_CMD_CHIP_CONFIG), payload
