"""Family decoder for ath9k_htc (AR9271): dump text, channel-switch and TX trigger recognition."""
from __future__ import annotations

import struct
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbTransfer
from tests.verify_pcap.verify_capture import FamilyDecoder, InjectFrameTrigger, SetChannelTrigger, Trigger
from wifit3.chips.ar9271_v2 import chan, constants as C, reg as R, tx, wmi
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver

CHANNEL_SWITCH_LOOKAHEAD = 600
DESCRIBED_REGISTER_COUNT = 3

WMI_COMMAND_NAMES = {
    wmi.WMI_GET_FW_VERSION_CMDID: "GET_FW_VERSION", wmi.WMI_DISABLE_INTR_CMDID: "DISABLE_INTR",
    wmi.WMI_ENABLE_INTR_CMDID: "ENABLE_INTR", wmi.WMI_ATH_INIT_CMDID: "ATH_INIT",
    wmi.WMI_DRAIN_TXQ_ALL_CMDID: "DRAIN_TXQ_ALL", wmi.WMI_START_RECV_CMDID: "START_RECV",
    wmi.WMI_STOP_RECV_CMDID: "STOP_RECV", wmi.WMI_FLUSH_RECV_CMDID: "FLUSH_RECV",
    wmi.WMI_SET_MODE_CMDID: "SET_MODE", wmi.WMI_NODE_CREATE_CMDID: "NODE_CREATE",
    wmi.WMI_VAP_CREATE_CMDID: "VAP_CREATE", wmi.WMI_REG_READ_CMDID: "REG_READ",
    wmi.WMI_REG_WRITE_CMDID: "REG_WRITE", wmi.WMI_TARGET_IC_UPDATE_CMDID: "TARGET_IC_UPDATE",
    wmi.WMI_REG_RMW_CMDID: "REG_RMW", tx.WMI_TXSTATUS_EVENTID: "TXSTATUS",
}
HTC_MESSAGE_NAMES = {
    C.HTC_MSG_READY_ID: "READY", C.HTC_MSG_CONNECT_SERVICE_ID: "CONNECT_SERVICE",
    C.HTC_MSG_CONNECT_SERVICE_RESPONSE_ID: "CONNECT_SERVICE_RESPONSE",
    C.HTC_MSG_SETUP_COMPLETE_ID: "SETUP_COMPLETE", C.HTC_MSG_CONFIG_PIPE_ID: "CONFIG_PIPE",
    C.HTC_MSG_CONFIG_PIPE_RESPONSE_ID: "CONFIG_PIPE_RESPONSE",
}
REGISTER_NAMES = {
    R.AR_PHY_SYNTH_CONTROL: "SYNTH_CONTROL", R.AR_TSF_L32: "TSF_L32", R.AR_TSF_U32: "TSF_U32",
    R.AR_RX_FILTER: "RX_FILTER", R.AR_GPIO_IN_OUT: "GPIO_IN_OUT", R.AR_CFG: "CFG",
    R.AR_DEF_ANTENNA: "DEF_ANTENNA", R.AR_STA_ID0: "STA_ID0", R.AR_STA_ID1: "STA_ID1",
}
SYNTH_CHANNEL_SELECT_MASK = 0x00FFFFFF
CHANNEL_BY_SYNTH_SELECT = {R.CHANSEL_2G(chan.channel_2ghz(number).center_freq): number
                           for number in AR9271V2Driver.SUPPORTED_CHANNELS}


def wmi_command_id(transfer: UsbTransfer) -> int | None:
    """WMI command (OUT) or response / event (IN) id on the REG pipes; None for HTC control or other pipes."""
    if transfer.endpoint not in (C.EP_REG_OUT, C.EP_REG_IN) or len(transfer.data) < C.HTC_FRAME_HDR_LEN + 4:
        return None
    if transfer.data[0] == C.ENDPOINT0:
        return None
    return struct.unpack_from(">H", transfer.data, C.HTC_FRAME_HDR_LEN)[0]


def register_words(transfer: UsbTransfer) -> list[int]:
    payload = transfer.data[C.HTC_FRAME_HDR_LEN + 4:]
    return list(struct.unpack(f">{len(payload) // 4}I", payload[:len(payload) // 4 * 4]))


def register_text(address: int) -> str:
    name = REGISTER_NAMES.get(address)
    if name is None:
        return f"0x{address:04x}"
    return f"0x{address:04x} {name}"


class Ath9kHtcDecoder(FamilyDecoder):
    def describe(self, transfer: UsbTransfer) -> str:
        status = f"  status {transfer.status}" if transfer.status else ""
        if transfer.transfer_type == TransferType.CONTROL:
            assert transfer.setup is not None
            if transfer.request == C.FIRMWARE_DOWNLOAD:
                return f"FW download 0x{transfer.value << 8:06x} {len(transfer.data)}B{status}"
            if transfer.request == C.FIRMWARE_DOWNLOAD_COMP:
                return f"FW boot 0x{transfer.value << 8:06x}{status}"
            return f"CTRL setup {transfer.setup.hex()} data {transfer.data[:16].hex()}{status}"
        if transfer.endpoint == C.EP_WLAN_TX:
            frame_control = struct.unpack_from("<H", tx.dot11_from_bulk(transfer.data))[0]
            return (f"TX {len(transfer.data)}B epid {transfer.data[4]} "
                    f"header {transfer.data[:24].hex()} FC 0x{frame_control:04x}{status}")
        if transfer.endpoint == C.EP_WLAN_RX:
            return f"RX {len(transfer.data)}B{status}"
        direction = "<-" if transfer.is_in else "->"
        if transfer.status and not transfer.data:
            return f"{direction} ep 0x{transfer.endpoint:02x}{status}"
        if len(transfer.data) >= C.HTC_FRAME_HDR_LEN + 2 and transfer.data[0] == C.ENDPOINT0:
            message_id = struct.unpack_from(">H", transfer.data, C.HTC_FRAME_HDR_LEN)[0]
            name = HTC_MESSAGE_NAMES.get(message_id, f"0x{message_id:04x}")
            return f"{direction} HTC {name} {transfer.data[C.HTC_FRAME_HDR_LEN + 2:].hex()}{status}"
        command_id = wmi_command_id(transfer)
        if command_id is None:
            return f"{direction} ep 0x{transfer.endpoint:02x} {transfer.data.hex()}{status}"
        seq = struct.unpack_from(">H", transfer.data, C.HTC_FRAME_HDR_LEN + 2)[0]
        name = WMI_COMMAND_NAMES.get(command_id, f"0x{command_id:04x}")
        text = f"{direction} {name} seq {seq}"
        words = register_words(transfer)
        if transfer.is_in:
            if command_id == wmi.WMI_REG_READ_CMDID:
                values = " ".join(f"0x{word:08x}" for word in words[:DESCRIBED_REGISTER_COUNT])
                text += f" = {values}"
            elif words:
                text += f" {transfer.data[C.HTC_FRAME_HDR_LEN + 4:][:16].hex()}"
            return text + status
        if command_id == wmi.WMI_REG_READ_CMDID:
            entries = [register_text(word) for word in words]
        elif command_id == wmi.WMI_REG_WRITE_CMDID:
            entries = [f"{register_text(words[i])}=0x{words[i + 1]:08x}" for i in range(0, len(words) - 1, 2)]
        elif command_id == wmi.WMI_REG_RMW_CMDID:
            entries = [f"{register_text(words[i])} set 0x{words[i + 1]:08x} clr 0x{words[i + 2]:08x}"
                       for i in range(0, len(words) - 2, 3)]
        else:
            entries = [transfer.data[C.HTC_FRAME_HDR_LEN + 4:].hex()] if words else []
        text += " " + ", ".join(entries[:DESCRIBED_REGISTER_COUNT])
        if len(entries) > DESCRIBED_REGISTER_COUNT:
            text += f" +{len(entries) - DESCRIBED_REGISTER_COUNT}"
        return text + status

    def find_trigger(self, transfers: Sequence[UsbTransfer], position: int) -> Trigger | None:
        transfer = transfers[position]
        if transfer.transfer_type == TransferType.BULK and transfer.endpoint == C.EP_WLAN_TX:
            return InjectFrameTrigger(tx.dot11_from_bulk(transfer.data))
        # ath9k_htc_set_channel: DISABLE_INTR, then the reset / fast change that writes the synth.
        if wmi_command_id(transfer) != wmi.WMI_DISABLE_INTR_CMDID or transfer.is_in:
            return None
        for lookahead in transfers[position + 1:position + CHANNEL_SWITCH_LOOKAHEAD]:
            command_id = wmi_command_id(lookahead)
            if lookahead.is_in or command_id is None:
                continue
            if command_id == wmi.WMI_DISABLE_INTR_CMDID:
                return None
            if command_id != wmi.WMI_REG_WRITE_CMDID:
                continue
            words = register_words(lookahead)
            for i in range(0, len(words) - 1, 2):
                if words[i] == R.AR_PHY_SYNTH_CONTROL:
                    channel = CHANNEL_BY_SYNTH_SELECT.get(words[i + 1] & SYNTH_CHANNEL_SELECT_MASK)
                    if channel is None:
                        return None
                    return SetChannelTrigger(channel)
        return None

    def carries_only_80211_frames(self, transfer: UsbTransfer) -> bool:
        """WLAN_RX bulk IN is the HIF RX stream only; WMI responses and events (TXSTATUS) come on REG_IN."""
        return transfer.transfer_type == TransferType.BULK and transfer.endpoint == C.EP_WLAN_RX
