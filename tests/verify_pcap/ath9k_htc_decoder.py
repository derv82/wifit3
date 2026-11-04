"""Family decoder for ath9k_htc (AR9271): dump text, channel-switch and TX trigger recognition."""
from __future__ import annotations

import struct
from dataclasses import replace
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder, RegisterAccess, RegisterOp
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
WMI_SEQ_OFFSET = C.HTC_FRAME_HDR_LEN + 2
WMI_SEQ_COUNT = 0x10000
CHANNEL_BY_SYNTH_SELECT = {R.CHANSEL_2G(chan.channel_2ghz(number).center_freq): number
                           for number in AR9271V2Driver.SUPPORTED_CHANNELS}


def wmi_command_id(op: UsbOp) -> int | None:
    """WMI command (OUT) or response / event (IN) id on the REG pipes; None for HTC control or other pipes."""
    if op.endpoint not in (C.EP_REG_OUT, C.EP_REG_IN) or len(op.data) < C.HTC_FRAME_HDR_LEN + 4:
        return None
    if op.data[0] == C.ENDPOINT0:
        return None
    return struct.unpack_from(">H", op.data, C.HTC_FRAME_HDR_LEN)[0]


def wmi_seq(op: UsbOp) -> int:
    return struct.unpack_from(">H", op.data, WMI_SEQ_OFFSET)[0]


def with_wmi_seq(op: UsbOp, seq: int) -> UsbOp:
    return replace(op, data=op.data[:WMI_SEQ_OFFSET] + struct.pack(">H", seq) + op.data[WMI_SEQ_OFFSET + 2:])


def register_words(op: UsbOp) -> list[int]:
    payload = op.data[C.HTC_FRAME_HDR_LEN + 4:]
    return list(struct.unpack(f">{len(payload) // 4}I", payload[:len(payload) // 4 * 4]))


def register_text(address: int) -> str:
    name = REGISTER_NAMES.get(address)
    if name is None:
        return f"0x{address:04x}"
    return f"0x{address:04x} {name}"


class Ath9kHtcDecoder(FamilyDecoder):
    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        if op.transfer_type == TransferType.CONTROL:
            assert op.setup is not None
            if op.request == C.FIRMWARE_DOWNLOAD:
                return f"FW download 0x{op.value << 8:06x} {len(op.data)}B{status}"
            if op.request == C.FIRMWARE_DOWNLOAD_COMP:
                return f"FW boot 0x{op.value << 8:06x}{status}"
            return f"CTRL setup {op.setup.hex()} data {op.data[:16].hex()}{status}"
        if op.endpoint == C.EP_WLAN_TX:
            frame_control = struct.unpack_from("<H", tx.dot11_from_bulk(op.data))[0]
            return (f"TX {len(op.data)}B epid {op.data[4]} "
                    f"header {op.data[:24].hex()} FC 0x{frame_control:04x}{status}")
        if op.endpoint == C.EP_WLAN_RX:
            return f"RX {len(op.data)}B{status}"
        direction = "<-" if op.is_in else "->"
        if op.status and not op.data:
            return f"{direction} ep 0x{op.endpoint:02x}{status}"
        if len(op.data) >= C.HTC_FRAME_HDR_LEN + 2 and op.data[0] == C.ENDPOINT0:
            message_id = struct.unpack_from(">H", op.data, C.HTC_FRAME_HDR_LEN)[0]
            name = HTC_MESSAGE_NAMES.get(message_id, f"0x{message_id:04x}")
            return f"{direction} HTC {name} {op.data[C.HTC_FRAME_HDR_LEN + 2:].hex()}{status}"
        command_id = wmi_command_id(op)
        if command_id is None:
            return f"{direction} ep 0x{op.endpoint:02x} {op.data.hex()}{status}"
        seq = wmi_seq(op)
        name = WMI_COMMAND_NAMES.get(command_id, f"0x{command_id:04x}")
        text = f"{direction} {name} seq {seq}"
        words = register_words(op)
        if op.is_in:
            if command_id == wmi.WMI_REG_READ_CMDID:
                values = " ".join(f"0x{word:08x}" for word in words[:DESCRIBED_REGISTER_COUNT])
                text += f" = {values}"
            elif words:
                text += f" {op.data[C.HTC_FRAME_HDR_LEN + 4:][:16].hex()}"
            return text + status
        if command_id == wmi.WMI_REG_READ_CMDID:
            entries = [register_text(word) for word in words]
        elif command_id == wmi.WMI_REG_WRITE_CMDID:
            entries = [f"{register_text(words[i])}=0x{words[i + 1]:08x}" for i in range(0, len(words) - 1, 2)]
        elif command_id == wmi.WMI_REG_RMW_CMDID:
            entries = [f"{register_text(words[i])} set 0x{words[i + 1]:08x} clr 0x{words[i + 2]:08x}"
                       for i in range(0, len(words) - 2, 3)]
        else:
            entries = [op.data[C.HTC_FRAME_HDR_LEN + 4:].hex()] if words else []
        text += " " + ", ".join(entries[:DESCRIBED_REGISTER_COUNT])
        if len(entries) > DESCRIBED_REGISTER_COUNT:
            text += f" +{len(entries) - DESCRIBED_REGISTER_COUNT}"
        return text + status

    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        op = ops[position]
        if op.transfer_type == TransferType.BULK and op.endpoint == C.EP_WLAN_TX:
            return tx.dot11_from_bulk(op.data)
        return None

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        op = ops[position]
        # ath9k_htc_set_channel: DISABLE_INTR, then the reset / fast change that writes the synth.
        if wmi_command_id(op) != wmi.WMI_DISABLE_INTR_CMDID or op.is_in:
            return None
        for lookahead in ops[position + 1:position + CHANNEL_SWITCH_LOOKAHEAD]:
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
                    return channel
        return None

    def register_ops(self, op: UsbOp) -> list[RegisterOp]:
        """REG_READ / REG_WRITE / REG_RMW commands, a REG_RMW entry as its SET_BITS then CLEAR_BITS; responses and
        events list none."""
        command_id = wmi_command_id(op)
        if op.is_in or command_id is None:
            return []
        words = register_words(op)
        if command_id == wmi.WMI_REG_READ_CMDID:
            return [RegisterOp(RegisterAccess.READ, word, None) for word in words]
        if command_id == wmi.WMI_REG_WRITE_CMDID:
            return [RegisterOp(RegisterAccess.WRITE, words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
        register_ops: list[RegisterOp] = []
        if command_id == wmi.WMI_REG_RMW_CMDID:
            for i in range(0, len(words) - 2, 3):
                register_ops.append(RegisterOp(RegisterAccess.SET_BITS, words[i], words[i + 1]))
                register_ops.append(RegisterOp(RegisterAccess.CLEAR_BITS, words[i], words[i + 2]))
        return register_ops

    def skip_capture_only_ops(self, ops: list[UsbOp], positions: set[int]) -> tuple[list[UsbOp], set[int]]:
        """Each later WMI command and its response renumbered back by the skipped commands before it (one
        tx_seq_id, wmi.c:286-307); a skipped command's response is waived with it."""
        skipped_count = 0
        skipped_seqs: set[int] = set()
        renumbered_seq_by_seq: dict[int, int] = {}
        waived_positions = set(positions)
        renumbered_ops: list[UsbOp] = []
        for position, op in enumerate(ops):
            command_id = wmi_command_id(op)
            if command_id is not None and not op.is_in:
                seq = wmi_seq(op)
                if position in positions:
                    skipped_count += 1
                    skipped_seqs.add(seq)
                elif skipped_count:
                    renumbered_seq_by_seq[seq] = (seq - skipped_count) % WMI_SEQ_COUNT
                    op = with_wmi_seq(op, renumbered_seq_by_seq[seq])
            elif command_id is not None and not command_id & wmi.WMI_EVENT_BIT:
                seq = wmi_seq(op)
                if seq in skipped_seqs:
                    skipped_seqs.discard(seq)
                    waived_positions.add(position)
                elif seq in renumbered_seq_by_seq:
                    op = with_wmi_seq(op, renumbered_seq_by_seq[seq])
            renumbered_ops.append(op)
        return renumbered_ops, waived_positions

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """WLAN_RX bulk IN is the HIF RX stream only; WMI responses and events (TXSTATUS) come on REG_IN."""
        return op.transfer_type == TransferType.BULK and op.endpoint == C.EP_WLAN_RX
