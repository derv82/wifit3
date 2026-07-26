"""MT7925AU connac3 MCU command layer.

Two kernel pieces:

  * the command-id field encoding — the ``MCU_CMD`` / ``MCU_UNI_CMD`` / ``MCU_CE_CMD``
    / ``MCU_EXT_CMD`` macros (mt76_connac_mcu.h). A command is a 32-bit int packing
    id + ext-id + UNI/CE/QUERY/WA flags.
  * ``build_mcu_frame`` — mt7925_mcu_fill_message (mt7925/mcu.c:3459) plus the USB
    SDIO header + tail pad (mt7925u_mcu_send_message, mt7925/usb.c:21). It turns an
    encoded command + payload into the on-wire bytes.

The connac3 txd differs from connac2 (mt7921) in one dword: ``txd[1] = 0x00004000``
(HDR_FORMAT_CMD at GENMASK(15,14)), not connac2's LONG_FORMAT|... = 0x80010000.
Values come from constants.py (grepped from source), never typed from memory.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *

# Command-id field encoding (mt76_connac_mcu.h __MCU_CMD_FIELD_*).
_F_ID    = 0x000000FF   # GENMASK(7, 0)   command id
_F_EXTID = 0x0000FF00   # GENMASK(15, 8)  ext command id
_F_QUERY = 1 << 16      # BIT(16)
_F_UNI   = 1 << 17      # BIT(17)         unified command
_F_CE    = 1 << 18      # BIT(18)         offload (CE) command
_F_WA    = 1 << 19      # BIT(19)         destined for WA

MCU_CMD_EXT_CID = 0xED   # cid byte carried by every EXT command


def MCU_CMD(t):
    return t & _F_ID


def MCU_EXT_CMD(t):
    return MCU_CMD_EXT_CID | ((t << 8) & _F_EXTID)


def MCU_UNI_CMD(t):
    return _F_UNI | (t & _F_ID)


def MCU_CE_CMD(t):
    return _F_CE | (t & _F_ID)


def build_mcu_frame(cmd, payload, seq):
    """Port of mt7925_mcu_fill_message + the USB SDIO header and tail pad.

    Returns the on-wire bytes for ``cmd`` (an encoded command int) carrying
    ``payload``, stamped with the 4-bit ``seq``.

    Wire layout: ``[4B SDIO hdr][txd: 64B][payload][pad]``. The connac3 txd is a
    fixed 64 B (mt76_connac2_mcu_txd); ``->len`` is ``skb_len - 32``.
    """
    mcu_cmd = cmd & _F_ID
    uni = bool(cmd & _F_UNI)
    txd_size = MCU_TXD_SIZE
    skb_len = txd_size + len(payload)            # skb->len when the SDIO hdr is added

    t = SDIO_HDR_SIZE                            # txd starts after the 4-byte SDIO hdr
    frame = bytearray(t + txd_size + len(payload))

    # SDIO header (mt792x_skb_add_usb_sdio_hdr): tx_bytes = skb->len, pkt_type 0.
    struct.pack_into("<I", frame, 0, skb_len & 0xFFFF)

    # txd[0]/txd[1] (mt7925_mcu_fill_message).
    struct.pack_into("<I", frame, t + 0, TXD0_BASE | (skb_len & 0xFFFF))
    struct.pack_into("<I", frame, t + 4, TXD1_CMD)
    struct.pack_into("<H", frame, t + 32, (skb_len - 32) & 0xFFFF)   # mcu_txd->len

    if uni:
        struct.pack_into("<H", frame, t + 34, mcu_cmd)   # cid (le16)
        frame[t + 37] = MCU_PKT_ID
        frame[t + 39] = seq & 0xFF
        frame[t + 42] = MCU_S2D_H2N
        frame[t + 43] = MCU_CMD_UNI_EXT_ACK              # option
    else:
        struct.pack_into("<H", frame, t + 34, MCU_PQ_ID)
        frame[t + 36] = mcu_cmd                          # cid
        frame[t + 37] = MCU_PKT_ID
        ext_cid = (cmd & _F_EXTID) >> 8
        if ext_cid or (cmd & _F_CE):
            set_query = 0 if (cmd & _F_QUERY) else 1     # MCU_Q_QUERY / MCU_Q_SET
        else:
            set_query = MCU_Q_NA                         # plain download command
        frame[t + 38] = set_query
        frame[t + 39] = seq & 0xFF
        frame[t + 41] = ext_cid
        frame[t + 42] = MCU_S2D_H2N
        frame[t + 43] = 1 if ext_cid else 0              # ext_cid_ack

    frame[t + txd_size:] = payload

    # Tail pad: round_up(len, 4) + 4 (mt7925u_mcu_send_message, mt7925/usb.c:41).
    pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
    frame.extend(b"\x00" * pad)
    return bytes(frame)


# uni_txd.option = MCU_CMD_UNI_EXT_ACK = ACK | UNI | SET (used by M2+ UNI commands).
MCU_CMD_UNI_EXT_ACK = 0x07


# ===========================================================================
# Firmware-download command encoders. Each returns (cmd, payload) and cites its
# kernel source. The transport stamps the seq and frames via build_mcu_frame.
# ===========================================================================

def patch_sem_ctrl(get: bool):
    """mt76_connac_mcu_patch_sem_ctrl (mt76_connac_mcu.c:23) — MCU_CMD(PATCH_SEM_CONTROL).
    Payload { __le32 op }: PATCH_SEM_GET to acquire, PATCH_SEM_RELEASE to release."""
    op = PATCH_SEM_GET if get else PATCH_SEM_RELEASE
    return MCU_CMD(MCU_CMD_PATCH_SEM_CONTROL), struct.pack("<I", op)


def get_data_mode(info: int) -> int:
    """mt76_connac2_get_data_mode (mt76_connac_mcu.c:3071) — the DL_MODE for a patch
    section, from its info.sec_key_idx word (BE). mt7925 decodes the encryption type
    in bits [31:24]: PLAIN adds nothing, AES adds ENCRYPT|KEY_IDX|RESET_SEC_IV,
    SCRAMBLE adds ENCRYPT|ENCRY_MODE_SEL|RESET_SEC_IV."""
    mode = DL_MODE_NEED_RSP
    if info == PATCH_SEC_NOT_SUPPORT:
        return mode
    enc = (info & PATCH_SEC_ENC_TYPE_MASK) >> 24
    if enc == PATCH_SEC_ENC_TYPE_AES:
        mode |= DL_MODE_ENCRYPT
        mode |= ((info & PATCH_SEC_ENC_AES_KEY_MASK) << 1) & DL_MODE_KEY_IDX
        mode |= DL_MODE_RESET_SEC_IV
    elif enc == PATCH_SEC_ENC_TYPE_SCRAMBLE:
        mode |= DL_MODE_ENCRYPT | DL_CONFIG_ENCRY_MODE_SEL | DL_MODE_RESET_SEC_IV
    return mode


def gen_dl_mode(feature_set: int) -> int:
    """mt76_connac_mcu_gen_dl_mode (mt76_connac_mcu.h:1862) — the DL_MODE for a RAM
    region, from its feature_set byte. is_wa is always false for mt7925 USB."""
    mode = DL_MODE_NEED_RSP
    if feature_set & FW_FEATURE_SET_ENCRYPT:
        mode |= DL_MODE_ENCRYPT | DL_MODE_RESET_SEC_IV
    if feature_set & FW_FEATURE_ENCRY_MODE:
        mode |= DL_CONFIG_ENCRY_MODE_SEL
    mode |= (((feature_set & FW_FEATURE_SET_KEY_IDX) >> 1) << 1) & DL_MODE_KEY_IDX
    return mode


def init_download(addr: int, length: int, mode: int):
    """mt76_connac_mcu_init_download (mt76_connac_mcu.c:53) — { __le32 addr, len, mode }.
    Command is PATCH_START_REQ for the patch/RAM entry addresses, else
    TARGET_ADDRESS_LEN_REQ (mt7925 branch, :67-73)."""
    if addr in PATCH_START_ADDRS:
        cmd = MCU_CMD(MCU_CMD_PATCH_START_REQ)
    else:
        cmd = MCU_CMD(MCU_CMD_TARGET_ADDRESS_LEN_REQ)
    return cmd, struct.pack("<III", addr, length, mode)


def patch_finish():
    """mt76_connac_mcu_start_patch (mt76_connac_mcu.c:37) — MCU_CMD(PATCH_FINISH_REQ).
    Payload { u8 check_crc = 0; u8 reserved[3] }."""
    return MCU_CMD(MCU_CMD_PATCH_FINISH_REQ), struct.pack("<B3x", 0)


def fw_start(override_addr: int):
    """mt76_connac_mcu_start_firmware (mt76_connac_mcu.c:8) — MCU_CMD(FW_START_REQ).
    Payload { __le32 option, __le32 addr }. option = FW_START_OVERRIDE when any
    region set an override address (is_wa is always false for mt7925 USB)."""
    option = FW_START_OVERRIDE if override_addr else 0
    return MCU_CMD(MCU_CMD_FW_START_REQ), struct.pack("<II", option, override_addr)
