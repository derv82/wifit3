"""Shared helpers for the rtl8922au active-monitor (auto-ACK) investigation.

Throwaway diagnostics for the open item in RTL8922AU.md ("Active monitor (auto-ACK): open").
NOT imported by src/. The addr-cam SMA programmer lives here because the equivalent driver code
(firmware.h2c_addr_cam) was reverted; this keeps the tests self-contained. The byte layout is the
addr-cam v0 H2C, validated earlier against the wire.
"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import usb.core
import usb.util
import libusb_package

from wifit3.wlan.discovery import build_interfaces
from wifit3.chips.rtl8922au import firmware
from wifit3.chips.rtl8922au.constants import (
    ADDR_CAM_W1_LEN, ADDR_CAM_W2_VALID, ADDR_CAM_W9_SEC_ENT_MODE, ADDR_CAM_W12_BSSID_LEN,
    ADDR_CAM_W13_BSSID_VALID, ADDR_CAM_W13_BSSID_MASK, ADDR_CAM_ENT_SHORT_SIZE, BSSID_CAM_ENT_SIZE,
    RTW89_ADDR_CAM_SEC_NORMAL, RTW89_BSSID_MATCH_ALL, RTW89_NET_TYPE_NO_LINK,
    H2C_CAT_MAC, H2C_CL_MAC_ADDR_CAM_UPDATE, H2C_FUNC_MAC_ADDR_CAM_UPD,
    R_BE_PORT_CFG_P0, B_AX_NET_TYPE_MASK, B_AX_RX_BSSID_FIT_EN, B_AX_TSF_UDT_EN,
    R_BE_TRXPTCL_RESP_0, R_BE_RX_FLTR_OPT, B_BE_SNIFFER_MODE,
    B_BE_A_BC_CAM_MATCH, B_BE_A_UC_CAM_MATCH, B_BE_A_MC, B_BE_A_BC, B_BE_A_A1_MATCH,
    B_BE_UID_FILTER_MASK,
)

_MAC_BE_BAND_OFFSET = 0x4000                 # RTW89_MAC_BE_BAND_REG_OFFSET (mac_idx 1 shift)
# The monitor RX filter value rx_fltr_init writes, minus SNIFFER_MODE. [SRC] mac_be.c:1315.
_FLTR_BASE = (B_BE_A_BC_CAM_MATCH | B_BE_A_UC_CAM_MATCH | B_BE_A_MC | B_BE_A_BC
              | B_BE_A_A1_MATCH | (15 << 24))   # field_prep(UID_FILTER, 15)

VID, PID = 0x0B05, 0x1D84
PROBE_SRC = bytes.fromhex("02b0b0000001")   # prober injects as this; the DUT's ACK comes back to it
NET_NO_LINK = RTW89_NET_TYPE_NO_LINK        # 0
NET_ADHOC = 1                               # RTW89_NET_TYPE_AD_HOC: IBSS, ACKs unicast-to-self, no AP
NET_INFRA = 2                               # RTW89_NET_TYPE_INFRA (reverted out of constants.py)
B_BE_RSP_CHK_CCA = 1 << 23                  # R_BE_TRXPTCL_RESP_0 CCA check on responses. reg.h:7658


def reset_device():
    """USB port reset (re-enumerate). Note: does NOT cut power, so a wedged RX-DMA can survive it;
    a physical replug is the real recovery. Handy only for a light nudge."""
    b = libusb_package.get_libusb1_backend()
    d = usb.core.find(idVendor=VID, idProduct=PID, backend=b)
    if d is None:
        print("reset: device not found")
        return
    try:
        d.reset()
    except Exception as e:                                  # noqa: BLE001
        print("reset err:", e)
    usb.util.dispose_resources(d)
    time.sleep(5)


def pick(dut_sub="8922", prober_sub="8812"):
    ifaces = build_interfaces()
    dut = next((i for i in ifaces if dut_sub in (i.description or "").lower()), None)
    prober = next((i for i in ifaces if i is not dut and prober_sub in (i.description or "").lower()), None)
    if dut is None or prober is None:
        raise SystemExit(f"need a '{dut_sub}' DUT + a '{prober_sub}' prober; found: "
                         + ", ".join(i.description or "?" for i in ifaces))
    return ifaces, dut, prober


def _xor(mac):
    h = 0
    for b in mac:
        h ^= b
    return h


def program_sma(t, ep, sma, *, net_type=NET_NO_LINK, tma=b"\x00" * 6,
                bssid=b"\x00" * 6, bssid_mask=RTW89_BSSID_MATCH_ALL):
    """Program addr-cam entry 0 with SMA=sma. Sniffer mode is left ON (untouched), so the DUT keeps
    receiving all frames and the responder/A1-match can be observed on them."""
    w = [0] * 15
    w[1] = firmware._pb(ADDR_CAM_W1_LEN, ADDR_CAM_ENT_SHORT_SIZE)
    w[2] = (firmware._pb(ADDR_CAM_W2_VALID, 1) | ((net_type & 0x3) << 1)
            | (_xor(sma) << 16) | (_xor(tma) << 24))
    w[4] = int.from_bytes(sma[0:4], "little")                            # SMA0..3
    w[5] = int.from_bytes(sma[4:6], "little") | (int.from_bytes(tma[0:2], "little") << 16)
    w[6] = int.from_bytes(tma[2:6], "little")                           # TMA2..5
    w[9] = firmware._pb(ADDR_CAM_W9_SEC_ENT_MODE, RTW89_ADDR_CAM_SEC_NORMAL)
    w[12] = firmware._pb(ADDR_CAM_W12_BSSID_LEN, BSSID_CAM_ENT_SIZE)
    w[13] = (firmware._pb(ADDR_CAM_W13_BSSID_VALID, 1) | firmware._pb(ADDR_CAM_W13_BSSID_MASK, bssid_mask)
             | (bssid[0] << 16) | (bssid[1] << 24))
    w[14] = int.from_bytes(bssid[2:6], "little")
    firmware.h2c_command(t, ep, H2C_CAT_MAC, H2C_CL_MAC_ADDR_CAM_UPDATE, H2C_FUNC_MAC_ADDR_CAM_UPD,
                         struct.pack("<15I", *w), rack=False, dack=True)


def restore_monitor(t, ep):
    """Re-program the monitor baseline addr-cam (SMA=0)."""
    firmware.h2c_cam(t, ep)


def set_port_adhoc(t, mac_idx=0):
    """Flip port 0 from the monitor NO_LINK role to AD_HOC + the responder-enabling port bits.
    rtw89_mac_port_cfg_rx_sw / rx_sync_by_nettype set rx_sw + TSF_UDT for INFRA||AD_HOC; the monitor
    bring-up leaves them clear. AD_HOC is the IBSS role that ACKs unicast-to-self. [SRC] mac.c:4658."""
    reg = R_BE_PORT_CFG_P0                                  # port 0 (add the per-port offset for others)
    t.write32_mask(reg, B_AX_NET_TYPE_MASK, NET_ADHOC)
    t.write32_set(reg, B_AX_RX_BSSID_FIT_EN)               # rx_sw
    t.write32_set(reg, B_AX_TSF_UDT_EN)                    # rx_sync


def restore_port_monitor(t, mac_idx=0):
    """Undo set_port_adhoc: net_type back to NO_LINK, clear rx_sw + TSF_UDT."""
    reg = R_BE_PORT_CFG_P0
    t.write32_mask(reg, B_AX_NET_TYPE_MASK, NET_NO_LINK)
    t.write32_clr(reg, B_AX_RX_BSSID_FIT_EN)
    t.write32_clr(reg, B_AX_TSF_UDT_EN)


def set_sniffer(t, on):
    """Blind-write the monitor RX filter with/without SNIFFER_MODE on both MACs. Blind (no read)
    because a read of R_BE_RX_FLTR_OPT mid-RX-stream can wedge the RX DMA on this chip; the bring-up
    writes this filter blind too. With SMA programmed, sniffer OFF forces to-SMA frames to be
    accepted via A1/UC-CAM match (flagged to-me), the hypothesis being the responder skips
    promiscuously-accepted frames."""
    val = _FLTR_BASE | (B_BE_SNIFFER_MODE if on else 0)
    for reg in (R_BE_RX_FLTR_OPT, R_BE_RX_FLTR_OPT + _MAC_BE_BAND_OFFSET):
        t.write32(reg, val)


def clear_rsp_cca(t):
    """Clear the responder's CCA check (incl. BT-coex CCA) so a busy/BT channel can't silently eat
    ACKs on this combo part. Reversible only by a re-init. [SRC] reg.h:7658 B_BE_RSP_CHK_CCA."""
    t.write32_clr(R_BE_TRXPTCL_RESP_0, B_BE_RSP_CHK_CCA)


def silicon_mac(dut):
    return bytes(int(b, 16) for b in dut.driver.mac_address.split(":"))
