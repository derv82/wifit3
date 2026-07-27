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
from dataclasses import dataclass

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
    txd_size = UNI_TXD_SIZE if uni else MCU_TXD_SIZE   # sizeof(uni_txd)=48, sizeof(mcu_txd)=64
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
        option = MCU_CMD_UNI_QUERY_ACK if (cmd & _F_QUERY) else MCU_CMD_UNI_EXT_ACK
        # HIF_CTRL / CHIP_CONFIG do not request a fw reply (mt7925_mcu_fill_message).
        if mcu_cmd in (MCU_UNI_CMD_HIF_CTRL, MCU_UNI_CMD_CHIP_CONFIG):
            option &= ~MCU_CMD_ACK
        frame[t + 43] = option
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


# uni_txd fields (mt76_connac_mcu.h:1163-1169). option = ACK|UNI|SET for a set
# command, ACK|UNI for a query. UNI txd is 48 B (sizeof mt76_connac2_mcu_uni_txd).
UNI_TXD_SIZE        = 48
MCU_CMD_ACK           = 0x01   # BIT(0)
MCU_CMD_UNI_EXT_ACK   = 0x07   # MCU_CMD_ACK | MCU_CMD_UNI | MCU_CMD_SET
MCU_CMD_UNI_QUERY_ACK = 0x03   # MCU_CMD_ACK | MCU_CMD_UNI


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


# ===========================================================================
# Post-boot run_firmware tail + device init (connac3 UNI commands).
# ===========================================================================

def get_nic_capability():
    """mt7925_mcu_get_nic_capability (mt7925/mcu.c:924) — MCU_UNI_CMD(CHIP_CONFIG) QUERY.
    Payload { u8 _rsv[4]; __le16 tag=NIC_CAPA; __le16 len=4 }. The reply carries the
    card MAC + phy/band caps (parse_nic_capability)."""
    # Sent as a SET (not QUERY); CHIP_CONFIG clears the option ACK bit in build_mcu_frame.
    # The reply (with the same seq) still arrives on EP 0x84 and carries the caps.
    payload = struct.pack("<4xHH", UNI_CHIP_CONFIG_NIC_CAPA, 4)
    return MCU_UNI_CMD(MCU_UNI_CMD_CHIP_CONFIG), payload


def fw_log_2_host(ctrl: int):
    """mt7925_mcu_fw_log_2_host (mt7925/mcu.c:819) — MCU_UNI_CMD(WSYS_CONFIG).
    Payload { u8 _rsv[4]; __le16 tag=FW_LOG_CTRL; __le16 len=8; u8 ctrl; u8 interval; u8 _rsv2[2] }."""
    payload = struct.pack("<4xHHBB2x", UNI_WSYS_CONFIG_FW_LOG_CTRL, 8, ctrl, 0)
    return MCU_UNI_CMD(MCU_UNI_CMD_WSYS_CONFIG), payload


def set_eeprom():
    """mt7925_mcu_set_eeprom (mt7925/mcu.c:1477) — MCU_UNI_CMD(EFUSE_CTRL).
    Payload { u8 _rsv[4]; __le16 tag=BUFFER_MODE; __le16 len=8; u8 buffer_mode=EFUSE;
    u8 format=WHOLE; __le16 buf_len=0 }."""
    payload = struct.pack("<4xHHBBH", UNI_EFUSE_BUFFER_MODE, 8, EE_MODE_EFUSE, EE_FORMAT_WHOLE, 0)
    return MCU_UNI_CMD(MCU_UNI_CMD_EFUSE_CTRL), payload


@dataclass
class NicCaps:
    """Per-card capability from the GET_NIC_CAPAB reply (defaults = 2x2 dual-band)."""
    mac: "str | None" = None
    antenna_mask: int = 0x3
    has_2ghz: bool = True
    has_5ghz: bool = True
    has_6ghz: bool = False


# mt7925_mcu_parse_phy_cap struct byte order (mt7925/mcu.c:877): ht,vht,_5g,max_bw,
# nss,dbdc,tx_ldpc,rx_ldpc,tx_stbc,rx_stbc,hw_path,he,eht (all u8).
_PHY_CAP_NSS = 4
_PHY_CAP_HW_PATH = 10
_WF0_24G = 1 << 0
_WF0_5G = 1 << 1


def parse_nic_capability(resp: bytes) -> NicCaps:
    """Walk the GET_NIC_CAPAB reply for the per-card caps (mt7925/mcu.c:949-988). ``resp``
    is the raw response buffer; the reply body (mt76_connac_cap_hdr + {tag,len} TLVs)
    starts after the 44-byte mt7925_mcu_rxd. Each TLV's ``len`` includes its 4-byte
    header. Best-effort: fields with no TLV keep the reference defaults."""
    caps = NicCaps()
    if not resp or len(resp) < MT7925_RXD_HDR_SIZE + 4:
        return caps
    body = resp[MT7925_RXD_HDR_SIZE:]
    n_element = struct.unpack_from("<H", body, 0)[0]
    off = 4
    for _ in range(n_element):
        if off + 4 > len(body):
            break
        tag, tlv_len = struct.unpack_from("<HH", body, off)
        if tlv_len < 4 or off + tlv_len > len(body):
            break
        data = body[off + 4:off + tlv_len]
        if tag == MT_NIC_CAP_MAC_ADDR and len(data) >= 6:
            caps.mac = ":".join(f"{b:02x}" for b in data[:6])
        elif tag == MT_NIC_CAP_PHY and len(data) > _PHY_CAP_HW_PATH:
            nss = data[_PHY_CAP_NSS]
            hw_path = data[_PHY_CAP_HW_PATH]
            if nss:
                caps.antenna_mask = (1 << nss) - 1
            caps.has_2ghz = bool(hw_path & _WF0_24G)
            caps.has_5ghz = bool(hw_path & _WF0_5G)
        elif tag == MT_NIC_CAP_6G and len(data) >= 1:
            caps.has_6ghz = bool(data[0])
        off += tlv_len
    return caps


# ===========================================================================
# Monitor entry + channel tune (connac3 UNI). Byte layouts matched to the wire;
# DEV_INFO/BSS_INFO/SNIFFER are shared connac code (same as mt7921), rxfilter is
# the mt7925 UNI BAND_CONFIG form.
# ===========================================================================

# The monitor vif: band_idx 0xff, wcid = MT792x_WTBL_RESERVED (19).
_MON_BAND_IDX = 0xff
_MON_WCID = MT792x_WTBL_RESERVED


def uni_dev_info(active=True, omac_addr=b"\x00" * 6):
    """mt76_connac_mcu_uni_add_dev DEV_INFO half — MCU_UNI_CMD(DEV_INFO_UPDATE).
    hdr{omac_idx=0, band_idx=0xff, pad} + tlv{tag=DEV_INFO_ACTIVE, len=12, active,
    link_idx=0, omac_addr[6]}."""
    hdr = struct.pack("<BBH", 0, _MON_BAND_IDX, 0)
    tlv = struct.pack("<HHBB6s", DEV_INFO_ACTIVE, 12, 1 if active else 0, 0, omac_addr)
    return MCU_UNI_CMD(MCU_UNI_CMD_DEV_INFO_UPDATE), hdr + tlv


def uni_bss_info(active=True, bssid=b"\x00" * 6, conn_type=CONNECTION_INFRA_AP):
    """mt76_connac_mcu_uni_add_dev BSS_INFO half — MCU_UNI_CMD(BSS_INFO_UPDATE).
    hdr{bss_idx=0, pad[3]} + mt76_connac_bss_basic_tlv (32 B). band_idx 0xff,
    bmc_tx_wlan_idx = sta_idx = the monitor wcid (19)."""
    hdr = struct.pack("<B3x", 0)
    basic = struct.pack(
        "<HHBBBBIBB6sHHBBHHBB",
        UNI_BSS_INFO_BASIC, 32,                 # tag, len
        1 if active else 0, 0, 0, _MON_BAND_IDX,  # active, omac_idx, hw_bss_idx, band_idx
        conn_type,                              # conn_type
        1, 0,                                   # conn_state, wmm_idx
        bssid,
        _MON_WCID, 0,                           # bmc_tx_wlan_idx, bcn_interval
        0, 0,                                   # dtim_period, phymode
        _MON_WCID, 0,                           # sta_idx, nonht_basic_phy
        0, 0,                                   # phymode_ext, link_idx
    )
    return MCU_UNI_CMD(MCU_UNI_CMD_BSS_INFO_UPDATE), hdr + basic


def uni_bss_pm_disable():
    """mt7925_mcu_set_bss_pm(enable=false) (mt7925/mcu.c:1576) — BSS_INFO_UPDATE with a
    PM_DISABLE tlv. hdr{bss_idx=0, pad[3]} + {tag=PM_DISABLE, len=4}."""
    return (MCU_UNI_CMD(MCU_UNI_CMD_BSS_INFO_UPDATE),
            struct.pack("<B3xHH", 0, UNI_BSS_INFO_PM_DISABLE, 4))


def set_sniffer(enable, band_idx=0):
    """mt7925_mcu_set_sniffer — MCU_UNI_CMD(SNIFFER) enable TLV (tag 0).
    hdr{band_idx, pad[3]} + tlv{tag=ENABLE, len=8, enable, pad[3]}."""
    hdr = struct.pack("<B3x", band_idx)
    tlv = struct.pack("<HHB3x", UNI_SNIFFER_ENABLE, 8, 1 if enable else 0)
    return MCU_UNI_CMD(MCU_UNI_CMD_SNIFFER), hdr + tlv


def _ch_band(channel):
    return CH_BAND_2GHZ if channel <= 14 else CH_BAND_5GHZ


def config_sniffer(channel, band_idx=0):
    """mt7925_mcu_config_sniffer — MCU_UNI_CMD(SNIFFER) config TLV (tag 1), 20 MHz.
    hdr{band_idx, pad[3]} + tlv{tag=CONFIG, len=16, aid, ch_band, bw=0, control_ch,
    sco=0, center_ch, center_ch2=0, drop_err=1, pad[3]}. control_ch==center_ch for 20 MHz."""
    hdr = struct.pack("<B3x", band_idx)
    tlv = struct.pack("<HHHBBBBBBB3x",
                      UNI_SNIFFER_CONFIG, 16, 0,        # tag, len, aid
                      _ch_band(channel), 0,             # ch_band, bw
                      channel, 0, channel,              # control_ch, sco, center_ch
                      0, 1)                             # center_ch2, drop_err
    return MCU_UNI_CMD(MCU_UNI_CMD_SNIFFER), hdr + tlv


# World-"00" default regdom (net/wireless/reg.c world_regdom) applied to the mt76
# channel plan (mac80211.c mt76_channels_2ghz/5ghz). With no country set, cfg80211 uses
# region "00", whose per-channel flags are fixed and public; we encode that output
# directly (byte-identical to what Linux region-00 produces, and to the capture) rather
# than port cfg80211's regulatory engine. World "00" is: NO_IR (passive) on 2.4 GHz
# ch12-14 and every 5 GHz channel, DSSS-only on ch14, DFS/RADAR on 5 GHz 52-64 + 100-144,
# 2.4 GHz capped at 40 MHz (NO_80/160), and HT40+/- cleared at each sub-band edge. 6 GHz
# is fully world-disabled, so no 6 GHz channels are emitted. Every channel carries
# NO_320MHZ. Flag bits: constants.CHAN_*.
_B = CHAN_NO_320MHZ
_2G_40 = CHAN_NO_160MHZ | CHAN_NO_80MHZ          # 2.4 GHz capped at 40 MHz
_DFS = CHAN_RADAR | CHAN_NO_IR                    # 5 GHz DFS sub-bands
W00_2G = [
    (1,  _B | _2G_40 | CHAN_NO_HT40MINUS),
    (2,  _B | _2G_40 | CHAN_NO_HT40MINUS),
    (3,  _B | _2G_40 | CHAN_NO_HT40MINUS),
    (4,  _B | _2G_40 | CHAN_NO_HT40MINUS),
    (5,  _B | _2G_40),
    (6,  _B | _2G_40),
    (7,  _B | _2G_40),
    (8,  _B | _2G_40),
    (9,  _B | _2G_40),
    (10, _B | _2G_40 | CHAN_NO_HT40PLUS),
    (11, _B | _2G_40 | CHAN_NO_HT40PLUS),
    (12, _B | CHAN_NO_160MHZ | CHAN_NO_HT40PLUS | CHAN_NO_IR),   # 2467-2482 rule (NO-IR)
    (13, _B | CHAN_NO_160MHZ | CHAN_NO_HT40PLUS | CHAN_NO_IR),
    (14, _B | _2G_40 | CHAN_NO_OFDM | CHAN_NO_HT40MINUS | CHAN_NO_HT40PLUS | CHAN_NO_IR),
]
W00_5G = [
    (36,  _B | CHAN_NO_HT40MINUS),
    (40,  _B), (44, _B), (48, _B),
    (52,  _B | _DFS), (56, _B | _DFS), (60, _B | _DFS),
    (64,  _B | CHAN_NO_HT40PLUS | _DFS),
    (100, _B | CHAN_NO_HT40MINUS | _DFS),
    (104, _B | _DFS), (108, _B | _DFS), (112, _B | _DFS), (116, _B | _DFS),
    (120, _B | _DFS), (124, _B | _DFS), (128, _B | _DFS), (132, _B | _DFS),
    (136, _B | _DFS), (140, _B | _DFS),
    (144, _B | CHAN_NO_HT40PLUS | _DFS),
    (149, _B | CHAN_NO_160MHZ | CHAN_NO_HT40MINUS),
    (153, _B | CHAN_NO_160MHZ), (157, _B | CHAN_NO_160MHZ), (161, _B | CHAN_NO_160MHZ),
    (165, _B | CHAN_NO_160MHZ | CHAN_NO_HT40PLUS),
]


def set_channel_domain():
    """mt7925_mcu_set_channel_domain (mt7925/mcu.c:3249) for the world-"00" regdom.
    hdr{alpha2="00\\0\\0", bw_2g=0, bw_5g=3, bw_6g=3, pad} + n_ch{tag=2, len, n_2ch,
    n_5ch, n_6ch, pad} + per-channel{hw_value:le16, pad:le16, flags:le32}. len =
    sizeof(n_chan)(8) + n_channels*8 (mcu.c:3345). 6 GHz world-disabled: none emitted."""
    chans = b"".join(struct.pack("<HHI", hv, 0, fl) for hv, fl in W00_2G + W00_5G)
    hdr = struct.pack("<4sBBBx", b"00", 0, 3, 3)
    n_ch = struct.pack("<HHBBBx", 2, 8 + len(chans), len(W00_2G), len(W00_5G), 0)
    return MCU_UNI_CMD(MCU_UNI_CMD_SET_DOMAIN_INFO), hdr + n_ch + chans


def set_rts_thresh(val=MT_RTS_THRESH_DEFAULT, band_idx=0):
    """mt7925_mcu_set_rts_thresh (mt7925/mcu.c:3550) — MCU_UNI_CMD(BAND_CONFIG) RTS TLV.
    { band_idx; rsv[3]; tag=RTS_THRESHOLD; len=12; len_thresh=val; pkt_thresh=2 }."""
    payload = struct.pack("<B3xHHII", band_idx, UNI_BAND_CONFIG_RTS_THRESHOLD, 12, val, 0x2)
    return MCU_UNI_CMD(MCU_UNI_CMD_BAND_CONFIG), payload


def set_rxfilter(fif, bit_map=0, bit_op=0, band_idx=0):
    """mt7925_mcu_set_rxfilter — MCU_UNI_CMD(BAND_CONFIG) RX-filter TLV (tag 0x0C).
    { band_idx; rsv1[3]; tag; len=68; mode = fif?0:1; rsv2[3]; fif; bit_map; bit_op;
    pad[51] }. DIFFERS FROM mt7921 (CE SET_RX_FILTER, mode fif?1:2)."""
    mode = 0 if fif else 1
    payload = struct.pack("<B3xHHB3xIIB51x",
                          band_idx, UNI_BAND_CONFIG_SET_MAC80211_RX_FILTER, 68,
                          mode, fif, bit_map, bit_op)
    return MCU_UNI_CMD(MCU_UNI_CMD_BAND_CONFIG), payload


# mt7925_configure_filter flags (mt7925/main.c). Monitor sets ENABLE|CONTROL|OTHER_BSS.
MT_FILTER_ENABLE    = 1 << 31
MT_FILTER_FCSFAIL   = 1 << 2
MT_FILTER_CONTROL   = 1 << 5
MT_FILTER_OTHER_BSS = 1 << 6


def chip_config(cmd: bytes):
    """mt7925_mcu_chip_config (mt7925/mcu.c:994) — MCU_UNI_CMD(CHIP_CONFIG), CHIP_CFG tag.
    Carries mt76_connac_config { id; type; resp_type; data_size; resv; data[320] } with
    ``cmd`` an ASCII command string. data_size = strlen + 1 (the NUL)."""
    data = cmd.ljust(320, b"\x00")
    config = struct.pack("<HBBHH", 0, 0, 0, len(cmd) + 1, 0) + data
    payload = struct.pack("<4xHH", UNI_CHIP_CONFIG_CHIP_CFG, 4 + len(config)) + config
    return MCU_UNI_CMD(MCU_UNI_CMD_CHIP_CONFIG), payload


def set_deep_sleep(enable):
    """mt7925_mcu_set_deep_sleep (mt7925/mcu.c:1018). USB leaves ds_enable 0, so init
    sends "KeepFullPwr 1"."""
    return chip_config(b"KeepFullPwr %d" % (0 if enable else 1))


# mt7925_mcu_set_thermal_protect (mt7925/mcu.c:1028-1041): two hardcoded chip_config
# command strings (10 params each), Gband then Aband.
THERMAL_GBAND = b"ThermalProtGband 0 100 90 80 30 1 1 115 105 5"
THERMAL_ABAND = b"ThermalProtAband 1 100 90 80 30 1 1 115 105 5"


def thermal_gband():
    return chip_config(THERMAL_GBAND)


def thermal_aband():
    return chip_config(THERMAL_ABAND)
