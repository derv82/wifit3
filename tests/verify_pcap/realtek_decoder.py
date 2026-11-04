"""Family decoder for Realtek vendor-request chips (bRequest 0x05, wValue = address); rtl8xxxu triggers."""
from __future__ import annotations

import struct
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbTransfer
from tests.verify_pcap.verify_capture import FamilyDecoder, InjectFrameTrigger, SetChannelTrigger, Trigger
from wifit3.chips.rtl8192fu import constants as C

REALTEK_USB_CMD_REQ = 0x05
RXDESC24_SIZE = 24
RXDESC24_RPT_SEL = 1 << 28
TXDESC40_SIZE = 40
CHANNEL_TUNE_LOOKAHEAD = 400
LSSI_DATA_MASK = 0xFFFFF

REGISTER_NAMES = {
    C.REG_RF_CTRL: "RF_CTRL", C.REG_RCR: "RCR", C.REG_BEACON_CTRL: "BEACON_CTRL", C.REG_TXPAUSE: "TXPAUSE",
    C.REG_TX_AGC_A_CCK1_MCS32: "TX_AGC_A_CCK1_MCS32", C.REG_FPGA0_XA_LSSI_PARM: "FPGA0_XA_LSSI_PARM",
    C.REG_FPGA0_XB_LSSI_PARM: "FPGA0_XB_LSSI_PARM", C.REG_RXFLTMAP0: "RXFLTMAP0", C.REG_RXFLTMAP2: "RXFLTMAP2",
    C.REG_HMTFR: "HMTFR", C.REG_HMBOX_0: "HMBOX_0",
    C.REG_MCU_FW_DL: "MCU_FW_DL", C.REG_SYS_FUNC: "SYS_FUNC", C.REG_APS_FSMCO: "APS_FSMCO",
    C.REG_EFUSE_CTRL: "EFUSE_CTRL", C.REG_SYS_CFG: "SYS_CFG", C.REG_CR: "CR",
}


def is_register_access(transfer: UsbTransfer) -> bool:
    return (transfer.transfer_type == TransferType.CONTROL and transfer.setup is not None
            and transfer.request == REALTEK_USB_CMD_REQ)


def register_value(transfer: UsbTransfer) -> int | None:
    if not transfer.data or len(transfer.data) > 4:
        return None
    return int.from_bytes(transfer.data, "little")


def lssi_rf_write(transfer: UsbTransfer) -> tuple[int, int] | None:
    """(RF register, value) of an RF write through FPGA0_X[AB]_LSSI_PARM, else None."""
    if not is_register_access(transfer) or transfer.is_in or transfer.value not in (
            C.REG_FPGA0_XA_LSSI_PARM, C.REG_FPGA0_XB_LSSI_PARM):
        return None
    value = register_value(transfer)
    if value is None:
        return None
    return value >> C.FPGA0_LSSI_PARM_ADDR_SHIFT, value & LSSI_DATA_MASK


class RealtekDecoder(FamilyDecoder):
    def describe(self, transfer: UsbTransfer) -> str:
        status = f"  status {transfer.status}" if transfer.status else ""
        if transfer.transfer_type != TransferType.CONTROL:
            direction = "RX" if transfer.is_in else "TX"
            text = f"{direction} ep 0x{transfer.endpoint:02x} {len(transfer.data)}B"
            if transfer.is_in:
                text += f" [{' '.join(self.rx_packet_kinds(transfer))}]"
            elif len(transfer.data) >= TXDESC40_SIZE + 2:
                frame_control = struct.unpack_from("<H", transfer.data, TXDESC40_SIZE)[0]
                text += f" descriptor {transfer.data[:TXDESC40_SIZE].hex()} FC 0x{frame_control:04x}"
            return text + status
        assert transfer.setup is not None
        if not is_register_access(transfer):
            return f"CTRL setup {transfer.setup.hex()} data {transfer.data.hex()}{status}"
        address = transfer.value
        name = REGISTER_NAMES.get(address, "")
        width = transfer.length
        value = register_value(transfer)
        if transfer.is_in:
            if value is None:
                return f"R  0x{address:04x}/{width} {name}{status}"
            return f"R  0x{address:04x}/{width} {name} = 0x{value:0{width * 2}x}{status}"
        if value is None:
            return f"W  0x{address:04x} {name} <{len(transfer.data)}B> {transfer.data[:8].hex()}{status}"
        text = f"W  0x{address:04x}/{width} {name} = 0x{value:0{width * 2}x}"
        rf_write = lssi_rf_write(transfer)
        if rf_write is not None:
            text += f"  (RF 0x{rf_write[0]:02x} <- 0x{rf_write[1]:05x})"
        return text + status

    def find_trigger(self, transfers: Sequence[UsbTransfer], position: int) -> Trigger | None:
        transfer = transfers[position]
        if transfer.transfer_type == TransferType.BULK and not transfer.is_in:
            return InjectFrameTrigger(transfer.data[TXDESC40_SIZE:])
        # rtl8xxxu_config(CHANGE_CHANNEL): set_tx_power opens on TX_AGC_A_CCK1_MCS32, then the RF 0x18 tune.
        if not (is_register_access(transfer) and transfer.is_in
                and transfer.value == C.REG_TX_AGC_A_CCK1_MCS32):
            return None
        for lookahead in transfers[position:position + CHANNEL_TUNE_LOOKAHEAD]:
            rf_write = lssi_rf_write(lookahead)
            if rf_write is not None and rf_write[0] == C.RF6052_REG_MODE_AG:
                return SetChannelTrigger(rf_write[1] & 0xFF)
        return None

    def carries_only_80211_frames(self, transfer: UsbTransfer) -> bool:
        """Bulk IN with no rxdesc24 rpt_sel packet; C2H (rpt_sel) stays in the walk for the driver."""
        return transfer.transfer_type == TransferType.BULK and "C2H" not in self.rx_packet_kinds(transfer)

    @staticmethod
    def rx_packet_kinds(transfer: UsbTransfer) -> list[str]:
        """"RX" or "C2H" per rxdesc24 packet in a bulk-IN buffer (rtl8xxxu_parse_rxdesc24 walk)."""
        data = transfer.data
        kinds: list[str] = []
        offset = 0
        while offset + RXDESC24_SIZE <= len(data):
            dw0, _dw1, dw2 = struct.unpack_from("<III", data, offset)
            packet_length = dw0 & 0x3FFF
            driver_info_size = ((dw0 >> 16) & 0xF) * 8
            shift = (dw0 >> 24) & 0x3
            kinds.append("C2H" if dw2 & RXDESC24_RPT_SEL else "RX")
            offset += (packet_length + driver_info_size + shift + RXDESC24_SIZE + 7) // 8 * 8
        return kinds
