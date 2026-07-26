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
    B_BE_WLANCPU_FWDL_EN,
    PWR_POLL_ATTEMPTS,
)

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


def _fwdl_ready(v: int, wcpu_fwdl_done: bool) -> bool:
    """fwdl_get_status_be reduced to "is the status firmware-init-ready?". For the WCPU-FWDL-DONE
    check that is when the WLAN-CPU download-enable bit clears; for both checks the raw status
    field reading 3 also maps to init-ready (fwdl_status_map[3]). [SRC] mac_be.c:709-755."""
    if wcpu_fwdl_done and not (v & B_BE_WLANCPU_FWDL_EN):
        return True
    return _gb(v, 26, 0xF) == 3          # fwdl_status_map[3] == RTW89_FWDL_WCPU_FW_INIT_RDY


def _fw_check_rdy(t, wcpu_fwdl_done: bool) -> None:
    """rtw89_fw_check_rdy: poll WCPU_FW_CTRL until the status reads firmware-init-ready.
    [SRC] fw.c:106-138."""
    _poll(t, R_BE_WCPU_FW_CTRL, lambda v: _fwdl_ready(v, wcpu_fwdl_done))


def download_suit(t, h2c_ep: int, fw: bytes, info: dict) -> None:
    """rtw89_fw_download_suit: the 8922A secure-boot malloc write, the H2C path-ready wait, the
    header download, the DLFW path-ready wait, and the section downloads. [SRC] fw.c:1948-1981."""
    t.write32(R_BE_SECURE_BOOT_MALLOC_INFO, SECURE_BOOT_MALLOC_VALUE)
    _fwdl_check_path_ready(t, True)
    t.bulk_out(h2c_ep, build_hdr_packet(fw, info))
    _fwdl_check_path_ready(t, False)
    t.write32(R_AX_HALT_H2C_CTRL, 0)
    t.write32(R_AX_HALT_C2H_CTRL, 0)
    for pkt in build_body_packets(fw, info):
        t.bulk_out(h2c_ep, pkt)
    _fw_check_rdy(t, True)               # RTW89_FWDL_CHECK_WCPU_FWDL_DONE. [SRC] fw.c:1899-1900


def download(t, h2c_ep: int, cv: int) -> None:
    """rtw89_fw_download (NORMAL, include_bb=False) minus the CPU disable/enable done in mac.py:
    load the firmware suit, run the transfer, then wait for the FreeRTOS-ready status.
    [SRC] fw.c:1984-2047."""
    fw = load_fw_suit(cv)
    info = parse_hdr_v1(fw)
    download_suit(t, h2c_ep, fw, info)
    time.sleep(0.005)                                 # mdelay(5). [SRC] fw.c:2019
    _fw_check_rdy(t, False)                           # RTW89_FWDL_CHECK_FREERTOS_DONE
