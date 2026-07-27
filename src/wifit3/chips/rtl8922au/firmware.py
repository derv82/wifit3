"""RTL8922AU firmware download, ported from rtw89-7.2 (fw.c, usb.c, core.c).

The firmware file is a multi-firmware (mfw) container; the NORMAL sub-firmware for this chip
cut is extracted, its v1 header parsed, and the header + code sections are sent to the WLAN CPU
as H2C/fwdl packets over bulk-OUT. Each packet is a 24-byte TX descriptor, and for the header an
8-byte H2C command header, in front of the firmware bytes. See RTL8922AU.md.
"""

import struct
import time
from pathlib import Path

from .constants import (
    FW_ASSET, RTW89_MFW_SIG, RTW89_FW_NORMAL,
    H2C_DESC_SIZE, H2C_HEADER_LEN, RTW89_USB_MOD512_PADDING,
    BE_RXD_RPKT_LEN_MASK, BE_RXD_RPKT_TYPE_SHIFT, RTW89_CORE_RX_TYPE_H2C, RTW89_CORE_RX_TYPE_FWDL,
    H2C_HDR_CAT_MAC, H2C_CL_MAC_FWDL, H2C_HDR_CLASS_SHIFT, H2C_HDR_TOTAL_LEN_MASK,
    FW_HDR_V1_W5_HDR_SIZE_SHIFT, FW_HDR_V1_W6_SEC_NUM_SHIFT, FW_HDR_V1_W6_SEC_NUM_MASK,
    FW_HDR_V1_W6_DSP_CHKSUM, FW_HDR_V1_W7_PART_SIZE_MASK, FW_HDR_V1_W7_DYN_HDR,
    FWSECTION_HDR_V1_W1_SEC_SIZE_MASK, FWSECTION_HDR_V1_W1_SECTIONTYPE_SHIFT,
    FWSECTION_HDR_V1_W1_CHECKSUM, FWSECTION_HDR_V1_W2_MSSC_MASK,
    FWDL_SECURITY_SECTION_TYPE, FWDL_SECTION_CHKSUM_LEN, FWDL_SECURITY_SIGLEN,
    FWDL_SECURITY_CHKSUM_LEN, FWDL_MSS_POOL_DEFKEYSETS_SIZE, FORMATTED_MSSC,
    MSS_POOL_HDR_LEN, MSS_SIGNATURE,
    R_BE_SECURE_BOOT_MALLOC_INFO, SECURE_BOOT_MALLOC_VALUE,
    R_BE_WCPU_FW_CTRL, B_BE_H2C_PATH_RDY, B_BE_DLFW_PATH_RDY,
    R_AX_HALT_H2C_CTRL, R_AX_HALT_C2H_CTRL,
    B_BE_WLANCPU_FWDL_EN, B_BE_BBMCU0_FWDL_EN,
    RTW89_FW_ELEMENT_ID_BBMCU0, RTW89_FW_ELEMENT_ALIGN, FW_ELEMENT_HDR_SIZE,
    FW_ELEMENT_BBMCU_CV_OFFSET, RTW89_TXPWR_CONF_DFLT_RFE_TYPE,
    PWR_POLL_ATTEMPTS,
    R_BE_H2CREG_DATA0, R_BE_C2HREG_DATA0, R_BE_H2CREG_CTRL, B_BE_H2CREG_TRIGGER, R_BE_C2HREG_CTRL,
    R_BE_MAILBOX_COUNTER, B_MAILBOX_H2C_CNT_MASK, B_MAILBOX_C2H_CNT_MASK,
    RTW89_H2CREG_MAX, RTW89_C2HREG_MAX, RTW89_H2CREG_HDR_LEN, RTW89_C2HREG_HDR_LEN,
    RTW89_H2CREG_HDR_FUNC_MASK, RTW89_H2CREG_HDR_LEN_MASK,
    RTW89_C2HREG_HDR_FUNC_MASK,
    FWCMD_TYPE_H2C, H2C_CAT_MAC, H2C_CL_MAC_MEDIA_RPT, H2C_FUNC_NOTIFY_DBCC,
    H2C_HDR_CAT_MASK, H2C_HDR_CLASS_MASK, H2C_HDR_FUNC_MASK, H2C_HDR_DEL_TYPE_MASK,
    H2C_HDR_H2C_SEQ_MASK, H2C_HDR_REC_ACK, H2C_HDR_DONE_ACK,
    RTW89_H2C_NOTIFY_DBCC_EN,
    H2C_CL_BA_CAM, H2C_FUNC_MAC_BA_CAM_INIT, RTW89_H2C_BA_CAM_INIT_USERS_MASK,
    RTW89_H2C_BA_CAM_INIT_OFFSET_MASK, RTW89_H2C_BA_CAM_INIT_BAND_SEL,
    H2C_CL_MAC_FW_OFLD, H2C_FUNC_OFLD_CFG, H2C_OFLD_CFG,
    H2C_CL_MAC_FR_EXCHG, H2C_CL_MAC_ADDR_CAM_UPDATE, H2C_FUNC_MAC_ADDR_CAM_UPD,
    H2C_FUNC_MAC_JOININFO, H2C_FUNC_MAC_FWROLE_MAINTAIN, H2C_FUNC_MAC_DCTLINFO_UD_V2,
    H2C_FUNC_MAC_CCTLINFO_UD_G7, H2C_FUNC_MAC_MACID_PAUSE_SLEEP,
    ROLE_MAINTAIN_W0_MACID, ROLE_MAINTAIN_W0_WIFI_ROLE,
    JOININFO_W0_MACID, JOININFO_W0_OP, JOININFO_W0_WIFI_ROLE, JOININFO_W1_MLO_MODE,
    JOININFO_W1_EMLSR_PADDING, JOININFO_W1_EMLSR_TRANS_DELAY,
    JOININFO_EML_PADDING_DELAY_256US, JOININFO_EMLSR_TRANSITION_DELAY_256US,
    ADDR_CAM_W1_LEN, ADDR_CAM_W2_VALID, ADDR_CAM_W9_SEC_ENT_MODE, ADDR_CAM_W12_BSSID_LEN,
    ADDR_CAM_W13_BSSID_VALID, ADDR_CAM_W13_BSSID_MASK, ADDR_CAM_ENT_SHORT_SIZE,
    BSSID_CAM_ENT_SIZE, RTW89_ADDR_CAM_SEC_NORMAL, RTW89_BSSID_MATCH_ALL,
)


def _pb(mask: int, val: int) -> int:
    """FIELD_PREP: place `val` into `mask`'s field."""
    shift = (mask & -mask).bit_length() - 1
    return (val << shift) & mask

_ASSET_PATH = Path(__file__).resolve().parent / "assets" / FW_ASSET


def _gb(word: int, shift: int, mask: int) -> int:
    return (word >> shift) & mask


def load_fw_suit(cv: int) -> bytes:
    """rtw89_mfw_recognize: from the multi-firmware file, return the NORMAL sub-firmware whose
    cut is the closest at or below hal.cv (non-MP). [SRC] fw.c:562-666."""
    data = _ASSET_PATH.read_bytes()
    if data[0] != RTW89_MFW_SIG:
        return data                                   # legacy flat firmware
    fw_nr = data[1]
    best = None                                       # (cv, shift, size)
    for i in range(fw_nr):
        off = 16 + i * 16                             # mfw_hdr is 16 bytes, mfw_info is 16 bytes
        c, typ, mp = data[off], data[off + 1], data[off + 2]
        shift, size = struct.unpack_from("<II", data, off + 4)
        if typ == RTW89_FW_NORMAL and c <= cv and not mp and (best is None or best[0] < c):
            best = (c, shift, size)
    if best is None:
        raise RuntimeError("rtl8922au: no suitable NORMAL firmware in mfw file")
    return data[best[1]:best[1] + best[2]]


def _align(x: int, a: int) -> int:
    return (x + a - 1) & ~(a - 1)


def load_bbmcu_suit(cv: int) -> bytes:
    """rtw89_fw_recognize_elements -> __rtw89_fw_recognize_from_elm: return the first BBMCU0
    firmware element whose cut is at or below hal.cv. Elements follow the mfw container, each a
    32-byte header then its contents; BBMCU cut is at offset 24. [SRC] fw.c:783-806, 1562-1622."""
    data = _ASSET_PATH.read_bytes()
    fw_nr = data[1]
    off = 16 + (fw_nr - 1) * 16                       # last mfw_info entry
    shift, size = struct.unpack_from("<II", data, off + 4)
    offset = _align(shift + size, RTW89_FW_ELEMENT_ALIGN)   # mfw_get_size, aligned
    while offset + FW_ELEMENT_HDR_SIZE < len(data):
        elem_id, elm_size = struct.unpack_from("<II", data, offset)
        if elem_id == RTW89_FW_ELEMENT_ID_BBMCU0 and data[offset + FW_ELEMENT_BBMCU_CV_OFFSET] <= cv:
            start = offset + FW_ELEMENT_HDR_SIZE
            return data[start:start + elm_size]
        offset = _align(offset + FW_ELEMENT_HDR_SIZE + elm_size, RTW89_FW_ELEMENT_ALIGN)
    raise RuntimeError("rtl8922au: no suitable BBMCU0 firmware element")


def element_regs_with_idx(elem_id: int, hal_aid: int = 0) -> tuple:
    """rtw89_build_phy_tbl_from_elm: the reg2 idx byte and (addr, data) pairs of the first firmware
    element with `elem_id` whose aid is 0 or matches hal_aid. Elements follow the mfw container,
    each a 32-byte header (24 fixed + 8-byte reg2 idx/rsvd prefix) then size/8 little-endian reg
    pairs. The idx byte is the radio table's storage slot. [SRC] fw.c:1081-1160, 1562-1622."""
    data = _ASSET_PATH.read_bytes()
    fw_nr = data[1]
    off = 16 + (fw_nr - 1) * 16
    shift, size = struct.unpack_from("<II", data, off + 4)
    offset = _align(shift + size, RTW89_FW_ELEMENT_ALIGN)
    while offset + FW_ELEMENT_HDR_SIZE < len(data):
        eid, elm_size = struct.unpack_from("<II", data, offset)
        aid = struct.unpack_from("<H", data, offset + 12)[0]
        if eid == elem_id and (aid == 0 or aid == hal_aid):
            idx = data[offset + 24]                    # reg2.idx, the first union byte
            start = offset + FW_ELEMENT_HDR_SIZE       # past the 8-byte reg2 idx/rsvd prefix
            n = elm_size // 8
            regs = [struct.unpack_from("<II", data, start + i * 8) for i in range(n)]
            return idx, regs
        offset = _align(offset + FW_ELEMENT_HDR_SIZE + elm_size, RTW89_FW_ELEMENT_ALIGN)
    raise RuntimeError(f"rtl8922au: no firmware element id={elem_id}")


def element_regs(elem_id: int, hal_aid: int = 0) -> list:
    """element_regs_with_idx without the idx byte, for tables that are not radio-slot-indexed."""
    return element_regs_with_idx(elem_id, hal_aid)[1]


def txpwr_conf(elem_id: int, rfe_type: int) -> tuple:
    """The FW txpwr element (ent_sz, num_ents, content bytes) for elem_id: prefer the entry whose
    rfe_type matches the efuse (last wins), else fall back to the default rfe_type 0. The txpwr
    sub-header sits at the union (offset 24): rsvd0, rsvd1, rfe_type, ent_sz, num_ents(le32),
    content. [SRC] fw.c:1165-1215, fw.h __rtw89_fw_txpwr_element."""
    data = _ASSET_PATH.read_bytes()
    fw_nr = data[1]
    off = 16 + (fw_nr - 1) * 16
    shift, size = struct.unpack_from("<II", data, off + 4)
    offset = _align(shift + size, RTW89_FW_ELEMENT_ALIGN)
    chosen = None                       # (rfe_type, ent_sz, num_ents, content)
    while offset + FW_ELEMENT_HDR_SIZE < len(data):
        eid, elm_size = struct.unpack_from("<II", data, offset)
        if eid == elem_id:
            e_rfe = data[offset + 26]
            ent_sz = data[offset + 27]
            num_ents = struct.unpack_from("<I", data, offset + 28)[0]
            start = offset + FW_ELEMENT_HDR_SIZE
            content = data[start:start + num_ents * ent_sz]
            if e_rfe == rfe_type:
                chosen = (e_rfe, ent_sz, num_ents, content)
            elif e_rfe == RTW89_TXPWR_CONF_DFLT_RFE_TYPE and (
                    chosen is None or chosen[0] == RTW89_TXPWR_CONF_DFLT_RFE_TYPE):
                chosen = (e_rfe, ent_sz, num_ents, content)
        offset = _align(offset + FW_ELEMENT_HDR_SIZE + elm_size, RTW89_FW_ELEMENT_ALIGN)
    if chosen is None:
        raise RuntimeError(f"rtl8922au: no txpwr element id={elem_id} rfe={rfe_type}")
    return chosen[1], chosen[2], chosen[3]


def parse_hdr_v1(fw: bytes) -> dict:
    """rtw89_fw_hdr_parser_v1 + the per-section parse (including the formatted-MSSC security
    handling that marks the second key variant `ignore`). [SRC] fw.c:404-537.
    Returns {section_num, part_size, hdr_len, dynamic_hdr_len, dsp_checksum, sections}, where each
    section is {addr, len, type, ignore}. secure_boot is off on this card, so no key copy."""
    w = struct.unpack_from("<12I", fw, 0)
    section_num = _gb(w[6], FW_HDR_V1_W6_SEC_NUM_SHIFT, 0xFF)
    dsp_checksum = bool(w[6] & FW_HDR_V1_W6_DSP_CHKSUM)
    part_size = w[7] & FW_HDR_V1_W7_PART_SIZE_MASK
    dyn_hdr_en = bool(w[7] & FW_HDR_V1_W7_DYN_HDR)
    base_hdr_len = 48 + section_num * 16              # struct_size(fw_hdr, sections, section_num)
    if dyn_hdr_en:
        hdr_len = _gb(w[5], FW_HDR_V1_W5_HDR_SIZE_SHIFT, 0xFFFF)
        dynamic_hdr_len = hdr_len - base_hdr_len
    else:
        hdr_len = base_hdr_len
        dynamic_hdr_len = 0

    sections = []
    binp = hdr_len
    secure_section_exist = False
    for i in range(section_num):
        s = struct.unpack_from("<4I", fw, 48 + i * 16)
        sec_type = _gb(s[1], FWSECTION_HDR_V1_W1_SECTIONTYPE_SHIFT, 0xF)
        length = s[1] & FWSECTION_HDR_V1_W1_SEC_SIZE_MASK
        if s[1] & FWSECTION_HDR_V1_W1_CHECKSUM:
            length += FWDL_SECTION_CHKSUM_LEN
        ignore = False
        mssc_len = 0
        if sec_type == FWDL_SECURITY_SECTION_TYPE:
            mssc = s[2] & FWSECTION_HDR_V1_W2_MSSC_MASK
            if (mssc & FORMATTED_MSSC) == FORMATTED_MSSC:
                mssc_len, ignore, secure_section_exist = _parse_formatted_mssc(
                    fw, binp, length, dsp_checksum, secure_section_exist)
            else:
                mssc_len = mssc * FWDL_SECURITY_SIGLEN
                if dsp_checksum:
                    mssc_len += mssc * FWDL_SECURITY_CHKSUM_LEN
        sections.append({"addr": binp, "len": length, "type": sec_type, "ignore": ignore})
        binp += length + mssc_len

    return {"section_num": section_num, "part_size": part_size, "hdr_len": hdr_len,
            "dynamic_hdr_len": dynamic_hdr_len, "dsp_checksum": dsp_checksum, "sections": sections}


def _parse_formatted_mssc(fw: bytes, content: int, length: int, dsp_checksum: bool,
                          secure_section_exist: bool) -> tuple:
    """__parse_formatted_mssc for the secure-boot-off path: size the MSS key pool that trails the
    section, and mark the section `ignore` if an earlier security section already exists.
    [SRC] fw.c:282-362. Returns (mssc_len, ignore, secure_section_exist)."""
    mh = content + length
    if fw[mh:mh + len(MSS_SIGNATURE)] != MSS_SIGNATURE:
        raise RuntimeError("rtl8922au: wrong MSS signature")
    defen = fw[mh + 16]
    mssdev_max = fw[mh + 21]
    keypair_num = struct.unpack_from("<H", fw, mh + 22)[0]
    msscust_max = struct.unpack_from("<H", fw, mh + 24)[0]
    msskey_num_max = struct.unpack_from("<H", fw, mh + 26)[0]
    rmp_tbl_size = (msskey_num_max * msscust_max * mssdev_max) >> 3   # MSS_POOL_RMP_TBL_BITMASK
    if defen:
        rmp_tbl_size += FWDL_MSS_POOL_DEFKEYSETS_SIZE
    key_sign_len = struct.unpack_from("<H", fw, content + 60)[0] >> 2  # section_content.key_sign_len
    if not key_sign_len:
        key_sign_len = 512
    if dsp_checksum:
        key_sign_len += FWDL_SECURITY_CHKSUM_LEN
    mssc_len = MSS_POOL_HDR_LEN + rmp_tbl_size + keypair_num * key_sign_len
    # secure_boot is off, so the key selection is skipped; the first security section marks
    # secure_section_exist, later ones are ignored. [SRC] fw.c:331-356.
    if secure_section_exist:
        return mssc_len, True, secure_section_exist
    return mssc_len, False, True


def _txd(pkt_size: int, fwdl: bool) -> bytes:
    """rtw89_build_txwd_fwcmd0_v2 + the zeroed rxdesc_short_v2 body. [SRC] core.c:1892-1900."""
    rx_type = RTW89_CORE_RX_TYPE_FWDL if fwdl else RTW89_CORE_RX_TYPE_H2C
    dword0 = (pkt_size & BE_RXD_RPKT_LEN_MASK) | (rx_type << BE_RXD_RPKT_TYPE_SHIFT)
    return struct.pack("<I", dword0) + b"\x00" * (H2C_DESC_SIZE - 4)


def _h2c_fwdl_hdr(payload_len: int) -> bytes:
    """rtw89_h2c_pkt_set_hdr_fwdl: 8-byte FWDL command header, h2c_seq 0. [SRC] fw.c:1649-1666."""
    hdr0 = H2C_HDR_CAT_MAC | (H2C_CL_MAC_FWDL << H2C_HDR_CLASS_SHIFT)
    hdr1 = (payload_len + H2C_HEADER_LEN) & H2C_HDR_TOTAL_LEN_MASK
    return struct.pack("<II", hdr0, hdr1)


def _pad_mod512(payload: bytes) -> bytes:
    """rtw89_usb_tx_write_fwcmd: avoid a whole-packet size that is a multiple of 512 by appending
    a small pad. [SRC] usb.c:370-391."""
    if (len(payload) + H2C_DESC_SIZE) % 512 == 0:
        return payload + b"\x00" * RTW89_USB_MOD512_PADDING
    return payload


def build_hdr_packet(fw: bytes, info: dict) -> bytes:
    """__rtw89_fw_download_hdr + __rtw89_fw_download_tweak_hdr_v1: the header packet is the first
    (hdr_len - dynamic_hdr_len) bytes of the firmware, with part_size written to w7, the ignored
    sections compacted out, w6 section count updated, and the removed section headers trimmed.
    [SRC] fw.c:1692-1775."""
    length = info["hdr_len"] - info["dynamic_hdr_len"]
    hdr = bytearray(fw[:length])
    w7 = struct.unpack_from("<I", hdr, 28)[0]
    struct.pack_into("<I", hdr, 28, (w7 & ~FW_HDR_V1_W7_PART_SIZE_MASK) | info["part_size"])
    dst = 0
    for i, sec in enumerate(info["sections"]):
        if sec["ignore"]:
            continue
        if dst != i:
            hdr[48 + dst * 16:48 + dst * 16 + 16] = hdr[48 + i * 16:48 + i * 16 + 16]
        dst += 1
    w6 = struct.unpack_from("<I", hdr, 24)[0]
    struct.pack_into("<I", hdr, 24, (w6 & ~FW_HDR_V1_W6_SEC_NUM_MASK)
                     | (dst << FW_HDR_V1_W6_SEC_NUM_SHIFT))
    truncated = (info["section_num"] - dst) * 16
    hdr = bytes(hdr[:length - truncated])
    payload = _pad_mod512(_h2c_fwdl_hdr(len(hdr)) + hdr)
    return _txd(len(payload), False) + payload


def build_body_packets(fw: bytes, info: dict) -> list:
    """__rtw89_fw_download_main: send each non-ignored section in part_size chunks, each behind a
    fwdl TX descriptor. [SRC] fw.c:1802-1863."""
    part_size = info["part_size"]
    packets = []
    for sec in info["sections"]:
        if sec["ignore"]:
            continue
        section = fw[sec["addr"]:sec["addr"] + sec["len"]]
        pos = 0
        residue = sec["len"]
        while residue:
            chunk = section[pos:pos + min(residue, part_size)]
            payload = _pad_mod512(chunk)
            packets.append(_txd(len(payload), True) + payload)
            pos += len(chunk)
            residue -= len(chunk)
    return packets


def _poll(t, addr: int, cond) -> int:
    for _ in range(PWR_POLL_ATTEMPTS):
        val = t.read32(addr)
        if cond(val):
            return val
    raise TimeoutError(f"rtl8922au: firmware poll timeout on 0x{addr:04x}")


def _fwdl_check_path_ready(t, h2c_or_fwdl: bool) -> None:
    """rtw89_fwdl_check_path_ready_be. [SRC] mac_be.c:757-766."""
    check = B_BE_H2C_PATH_RDY if h2c_or_fwdl else B_BE_DLFW_PATH_RDY
    _poll(t, R_BE_WCPU_FW_CTRL, lambda v: v & check)


def _fwdl_ready(v: int, done_mask: int) -> bool:
    """fwdl_get_status_be reduced to "is the status firmware-init-ready?". The per-type download
    -enable bit clearing (WLAN-CPU / BBMCU0) reports ready; for every check the raw status field
    reading 3 also maps to init-ready (fwdl_status_map[3]). [SRC] mac_be.c:709-755."""
    if done_mask and not (v & done_mask):
        return True
    return _gb(v, 26, 0xF) == 3          # fwdl_status_map[3] == RTW89_FWDL_WCPU_FW_INIT_RDY


def _fw_check_rdy(t, done_mask: int) -> None:
    """rtw89_fw_check_rdy: poll WCPU_FW_CTRL until the status reads firmware-init-ready. done_mask
    is the download-enable bit for the suit type (0 for the FreeRTOS-done check, which is
    status-only). [SRC] fw.c:106-138, mac_be.c:730-745."""
    _poll(t, R_BE_WCPU_FW_CTRL, lambda v: _fwdl_ready(v, done_mask))


def download_suit(t, h2c_ep: int, fw: bytes, info: dict, is_bbmcu: bool = False) -> None:
    """rtw89_fw_download_suit: the 8922A secure-boot malloc write (NORMAL/WOWLAN only), the H2C
    path-ready wait, the header download, the DLFW path-ready wait, and the section downloads. The
    trailing ready check is BB0-FWDL-DONE for a BBMCU suit, WCPU-FWDL-DONE otherwise.
    [SRC] fw.c:1948-1981, 1865-1908."""
    if not is_bbmcu:
        t.write32(R_BE_SECURE_BOOT_MALLOC_INFO, SECURE_BOOT_MALLOC_VALUE)   # fw.c:1963-1965
    _fwdl_check_path_ready(t, True)
    t.bulk_out(h2c_ep, build_hdr_packet(fw, info))
    _fwdl_check_path_ready(t, False)
    t.write32(R_AX_HALT_H2C_CTRL, 0)
    t.write32(R_AX_HALT_C2H_CTRL, 0)
    for pkt in build_body_packets(fw, info):
        t.bulk_out(h2c_ep, pkt)
    done_mask = B_BE_BBMCU0_FWDL_EN if is_bbmcu else B_BE_WLANCPU_FWDL_EN
    _fw_check_rdy(t, done_mask)          # BB0-FWDL-DONE / WCPU-FWDL-DONE. [SRC] fw.c:1865-1900


def download(t, h2c_ep: int, cv: int, include_bb: bool = False) -> None:
    """rtw89_fw_download (NORMAL) minus the CPU disable/enable done in mac.py: load the NORMAL
    firmware suit, run the transfer, then (when include_bb) the BB-MCU suit transfers, reset the
    mailbox counters, and wait for the FreeRTOS-ready status. [SRC] fw.c:1984-2047."""
    fw = load_fw_suit(cv)
    info = parse_hdr_v1(fw)
    download_suit(t, h2c_ep, fw, info)
    if include_bb:                                    # bbmcu_nr=1: one BBMCU0 suit. [SRC] fw.c:2004-2010
        bbmcu = load_bbmcu_suit(cv)
        download_suit(t, h2c_ep, bbmcu, parse_hdr_v1(bbmcu), is_bbmcu=True)
    t.h2c_seq = 0                                     # fw_info reset. [SRC] fw.c:2012-2015
    t.h2c_counter = 0
    t.c2h_counter = 0
    time.sleep(0.005)                                 # mdelay(5). [SRC] fw.c:2019
    _fw_check_rdy(t, 0)                               # RTW89_FWDL_CHECK_FREERTOS_DONE (status-only)


def h2c_command(t, h2c_ep: int, cat: int, cls: int, func: int, payload: bytes,
                rack: bool = False, dack: bool = True) -> None:
    """rtw89_h2c_pkt_set_hdr + rtw89_h2c_tx (USB fwcmd): an 8-byte fwcmd header (BE, so no AX
    rack override) plus the payload behind the H2C TX descriptor, sent on the H2C bulk-OUT.
    [SRC] fw.c:1624-1647, core.c:1336-1375, usb.c:360-399, core.c:1892-1900."""
    total = len(payload) + H2C_HEADER_LEN
    hdr0 = (_pb(H2C_HDR_DEL_TYPE_MASK, FWCMD_TYPE_H2C) | _pb(H2C_HDR_CAT_MASK, cat)
            | _pb(H2C_HDR_CLASS_MASK, cls) | _pb(H2C_HDR_FUNC_MASK, func)
            | _pb(H2C_HDR_H2C_SEQ_MASK, t.h2c_seq))
    hdr1 = (_pb(H2C_HDR_TOTAL_LEN_MASK, total) | (H2C_HDR_REC_ACK if rack else 0)
            | (H2C_HDR_DONE_ACK if dack else 0))
    t.h2c_seq = (t.h2c_seq + 1) & 0xFF
    fwcmd = struct.pack("<II", hdr0, hdr1) + payload
    pkt = _pad_mod512(fwcmd)
    t.bulk_out(h2c_ep, _txd(len(pkt), False) + pkt)


def h2c_notify_dbcc(t, h2c_ep: int, en: bool) -> None:
    """rtw89_fw_h2c_notify_dbcc: tell the running fw that band 1 (DBCC) is up. [SRC] fw.c."""
    w0 = _pb(RTW89_H2C_NOTIFY_DBCC_EN, 1 if en else 0)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_MEDIA_RPT, H2C_FUNC_NOTIFY_DBCC,
                struct.pack("<I", w0), rack=False, dack=True)


def h2c_init_ba_cam_users(t, h2c_ep: int, users: int, offset: int, mac_idx: int) -> None:
    """rtw89_fw_h2c_init_ba_cam_users: seed the BA-CAM user/offset/band for a MAC. [SRC] fw.c."""
    w0 = (_pb(RTW89_H2C_BA_CAM_INIT_USERS_MASK, users)
          | _pb(RTW89_H2C_BA_CAM_INIT_OFFSET_MASK, offset)
          | _pb(RTW89_H2C_BA_CAM_INIT_BAND_SEL, mac_idx))
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_BA_CAM, H2C_FUNC_MAC_BA_CAM_INIT,
                struct.pack("<I", w0), rack=False, dack=True)


def h2c_fw_log(t, h2c_ep: int, enable: bool) -> None:
    """rtw89_fw_h2c_fw_log: level LOUD, path C2H, component bitmap only when enabled (off at cold
    boot). cat MAC, class FW_INFO, func LOG_CFG. [SRC] fw.c:rtw89_fw_h2c_fw_log."""
    comp = 0x0000012D if enable else 0        # INIT|TASK|PS|ERROR|MLO|SCAN when enabled
    payload = struct.pack("<III", 0x00000204, comp, 0)   # w0 = LEVEL_LOUD | PATH(BIT C2H)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, 0x0, 0x0, payload, rack=False, dack=False)


def h2c_set_ofld_cfg(t, h2c_ep: int) -> None:
    """rtw89_fw_h2c_set_ofld_cfg: a fixed 8-byte offload config. [SRC] fw.c:5311."""
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_FW_OFLD, H2C_FUNC_OFLD_CFG,
                H2C_OFLD_CFG, rack=False, dack=True)


def h2c_macid_pause(t, h2c_ep: int, sh: int, grp: int, pause: bool) -> None:
    """rtw89_fw_h2c_macid_pause (MACID_PAUSE_SLEEP feature): struct of 4 per-group u32 arrays
    (pause/pause_mask/sleep/sleep_mask) in n[0]; only the mask arrays are set when not pausing.
    [SRC] fw.c:5172-5217, fw.h:320."""
    payload = bytearray(256)
    setbit = 1 << sh
    struct.pack_into("<I", payload, 16 + grp * 4, setbit)   # n[0].pause_mask_grp[grp]
    struct.pack_into("<I", payload, 48 + grp * 4, setbit)   # n[0].sleep_mask_grp[grp]
    if pause:
        struct.pack_into("<I", payload, 0 + grp * 4, setbit)    # n[0].pause_grp[grp]
        struct.pack_into("<I", payload, 32 + grp * 4, setbit)   # n[0].sleep_grp[grp]
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_FW_OFLD, H2C_FUNC_MAC_MACID_PAUSE_SLEEP,
                bytes(payload), rack=True, dack=False)


def h2c_role_maintain(t, h2c_ep: int, macid: int, wifi_role: int) -> None:
    """rtw89_fw_h2c_role_maintain(ROLE_CREATE): w0 of macid/self_role/upd_mode/wifi_role/band/
    port; a monitor create leaves all but macid and wifi_role at 0. [SRC] fw.c:4937, fw.h:1813."""
    w0 = _pb(ROLE_MAINTAIN_W0_MACID, macid) | _pb(ROLE_MAINTAIN_W0_WIFI_ROLE, wifi_role)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_MEDIA_RPT, H2C_FUNC_MAC_FWROLE_MAINTAIN,
                struct.pack("<I", w0), rack=False, dack=True)


def h2c_join_info(t, h2c_ep: int, macid: int, wifi_role: int, dis_conn: bool) -> None:
    """rtw89_fw_h2c_join_info (BE v1): w0 carries macid/op(dis_conn)/net_type/role; w1 the MLSR
    MLO mode + the EMLSR padding/transition 256us caps. [SRC] fw.c:5033-5122, fw.h:1837."""
    w0 = (_pb(JOININFO_W0_MACID, macid) | _pb(JOININFO_W0_OP, 1 if dis_conn else 0)
          | _pb(JOININFO_W0_WIFI_ROLE, wifi_role))
    w1 = (_pb(JOININFO_W1_MLO_MODE, 1)
          | _pb(JOININFO_W1_EMLSR_PADDING, JOININFO_EML_PADDING_DELAY_256US)
          | _pb(JOININFO_W1_EMLSR_TRANS_DELAY, JOININFO_EMLSR_TRANSITION_DELAY_256US))
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_MEDIA_RPT, H2C_FUNC_MAC_JOININFO,
                struct.pack("<III", w0, w1, 0), rack=False, dack=True)


def h2c_cam(t, h2c_ep: int) -> None:
    """rtw89_fw_h2c_cam(ROLE_CREATE, addrcam_ver 0): the monitor no-link addr-cam + bssid-cam.
    Every station/BSSID address is zero here; only the entry lengths, valid bits, sec-mode, and
    the match-all BSSID mask are set. [SRC] fw.c:2281, cam.c:768/819, cam.h:15."""
    w = [0] * 15
    w[1] = _pb(ADDR_CAM_W1_LEN, ADDR_CAM_ENT_SHORT_SIZE)
    w[2] = _pb(ADDR_CAM_W2_VALID, 1)
    w[9] = _pb(ADDR_CAM_W9_SEC_ENT_MODE, RTW89_ADDR_CAM_SEC_NORMAL)
    w[12] = _pb(ADDR_CAM_W12_BSSID_LEN, BSSID_CAM_ENT_SIZE)
    w[13] = _pb(ADDR_CAM_W13_BSSID_VALID, 1) | _pb(ADDR_CAM_W13_BSSID_MASK, RTW89_BSSID_MATCH_ALL)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_ADDR_CAM_UPDATE, H2C_FUNC_MAC_ADDR_CAM_UPD,
                struct.pack("<15I", *w), rack=False, dack=True)


# rtw89_fw_h2c_default_cmac_tbl_g7: the g7 default CMAC table (c0, w0..w15, m0..m15 = 33 u32).
# Only c0.MACID and w0.MGQ_RPT_EN (= hci.tx_rpt_enabled, always 1 for USB) are dynamic; the rest
# is the fixed default plus each word's _ALL write mask. [SRC] fw.c:3641-3706, fw.h:1404.
_CMAC_G7_WORDS = (
    0x00000080,                                                              # c0: MACID 0 | OP 1
    0x00200004, 0x400A0004, 0x0, 0x0, 0xFFFF0000, 0x000002AA, 0x0000B000,    # w0..w7
    0x000B8109, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,                      # w8..w15
    0xFFF7FFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFDFFF, 0xFFFF7FFF,   # m0..m5
    0x80FFFFFF, 0xEFFFEFFF, 0x0000FFFF, 0x0, 0x0, 0x0, 0x0, 0x0,             # m6..m13
    0xFFFFFFFF, 0x0FFFFFFF,                                                   # m14..m15
)

# rtw89_fw_h2c_default_dmac_tbl_v2: the v2 default DMAC table (c0, w0..w15, m0..m15 = 33 u32).
# All value words are 0 (only c0.MACID varies); m0..m12 are the _ALL write masks. [SRC] fw.c:2454.
_DMAC_V2_WORDS = (
    0x00000080,                                                              # c0: MACID 0 | OP 1
    0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,   # w0..w15
    0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFE0, 0xFFFFFF0F,   # m0..m5
    0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF,   # m6..m11
    0x0000FFFF, 0x0, 0x0, 0x0,                                                # m12..m15
)


_CCTL_C0_MACID = 0x7F                # CCTLINFO_G7_C0_MACID / DCTLINFO_V2_C0_MACID: GENMASK(6, 0)


def h2c_default_cmac_tbl(t, h2c_ep: int, macid: int = 0) -> None:
    """The g7 default CMAC table for a new role. [SRC] fw.c:3641."""
    words = list(_CMAC_G7_WORDS)
    words[0] = (words[0] & ~_CCTL_C0_MACID) | (macid & _CCTL_C0_MACID)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_FR_EXCHG, H2C_FUNC_MAC_CCTLINFO_UD_G7,
                struct.pack("<33I", *words), rack=False, dack=True)


def h2c_default_dmac_tbl(t, h2c_ep: int, macid: int = 0) -> None:
    """The v2 default DMAC table for a new role. [SRC] fw.c:2454."""
    words = list(_DMAC_V2_WORDS)
    words[0] = (words[0] & ~_CCTL_C0_MACID) | (macid & _CCTL_C0_MACID)
    h2c_command(t, h2c_ep, H2C_CAT_MAC, H2C_CL_MAC_FR_EXCHG, H2C_FUNC_MAC_DCTLINFO_UD_V2,
                struct.pack("<33I", *words), rack=False, dack=False)


def _write_h2c_reg(t, h2c_id: int, content_len: int, w0: int) -> None:
    """rtw89_fw_write_h2c_reg: wait for the H2C mailbox free, write the 4 H2CREG dwords, bump the
    H2C counter, and trigger. [SRC] fw.c:8229-8260."""
    for _ in range(PWR_POLL_ATTEMPTS):
        if t.read8(R_BE_H2CREG_CTRL) == 0:
            break
    length = (content_len + RTW89_H2CREG_HDR_LEN + 3) // 4     # DIV_ROUND_UP(., 4)
    w0 = (w0 & ~RTW89_H2CREG_HDR_FUNC_MASK) | (h2c_id & RTW89_H2CREG_HDR_FUNC_MASK)
    w0 = (w0 & ~RTW89_H2CREG_HDR_LEN_MASK) | ((length << 8) & RTW89_H2CREG_HDR_LEN_MASK)
    regs = [w0, 0, 0, 0]
    for i in range(RTW89_H2CREG_MAX):
        t.write32(R_BE_H2CREG_DATA0 + i * 4, regs[i])
    t.h2c_counter = (t.h2c_counter + 1) & 0xFF
    t.write8_mask(R_BE_MAILBOX_COUNTER, B_MAILBOX_H2C_CNT_MASK, t.h2c_counter)
    t.write8(R_BE_H2CREG_CTRL, B_BE_H2CREG_TRIGGER)


def _read_c2h_reg(t) -> dict:
    """rtw89_fw_read_c2h_reg: wait for the C2H mailbox ready, read the 4 C2HREG dwords, ack, bump
    the C2H counter, and decode the header. [SRC] fw.c:8262-8305."""
    for _ in range(PWR_POLL_ATTEMPTS):
        if t.read8(R_BE_C2HREG_CTRL):
            break
    regs = [t.read32(R_BE_C2HREG_DATA0 + i * 4) for i in range(RTW89_C2HREG_MAX)]
    t.write8(R_BE_C2HREG_CTRL, 0)
    cid = regs[0] & RTW89_C2HREG_HDR_FUNC_MASK
    content_len = (_gb(regs[0], 8, 0xF) << 2) - RTW89_C2HREG_HDR_LEN
    t.c2h_counter = (t.c2h_counter + 1) & 0xFF
    t.write8_mask(R_BE_MAILBOX_COUNTER, B_MAILBOX_C2H_CNT_MASK, t.c2h_counter)
    return {"id": cid, "content_len": content_len, "w": regs}


def msg_reg(t, h2c_id: int, content_len: int, w0: int) -> dict:
    """rtw89_fw_msg_reg: one register-mailbox round-trip (write H2CREG, read C2HREG).
    [SRC] fw.c:8307-8335."""
    _write_h2c_reg(t, h2c_id, content_len, w0)
    return _read_c2h_reg(t)
