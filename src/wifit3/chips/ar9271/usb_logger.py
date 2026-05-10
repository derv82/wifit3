import logging
import time
import struct
from dataclasses import dataclass

logger = logging.getLogger("usb_traffic")
logger.setLevel(logging.DEBUG)
logger.propagate = False # Prevent spamming root/console logger

# Ensure this logger dumps to a dedicated file and doesn't spam the UI
file_handler = logging.FileHandler("usb_transactions.log", mode='w')
formatter = logging.Formatter('%(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Auto-extracted from ath9k_htc C source code
WMI_COMMANDS = {
    0x0001: 'WMI_ECHO_CMDID',
    0x0002: 'WMI_ACCESS_MEMORY_CMDID',
    0x0003: 'WMI_GET_FW_VERSION',
    0x0004: 'WMI_DISABLE_INTR_CMDID',
    0x0005: 'WMI_ENABLE_INTR_CMDID',
    0x0006: 'WMI_ATH_INIT_CMDID',
    0x0007: 'WMI_ABORT_TXQ_CMDID',
    0x0008: 'WMI_STOP_TX_DMA_CMDID',
    0x0009: 'WMI_ABORT_TX_DMA_CMDID',
    0x000A: 'WMI_DRAIN_TXQ_CMDID',
    0x000B: 'WMI_DRAIN_TXQ_ALL_CMDID',
    0x000C: 'WMI_START_RECV_CMDID',
    0x000D: 'WMI_STOP_RECV_CMDID',
    0x000E: 'WMI_FLUSH_RECV_CMDID',
    0x000F: 'WMI_SET_MODE_CMDID',
    0x0010: 'WMI_NODE_CREATE_CMDID',
    0x0011: 'WMI_NODE_REMOVE_CMDID',
    0x0012: 'WMI_VAP_REMOVE_CMDID',
    0x0013: 'WMI_VAP_CREATE_CMDID',
    0x0014: 'WMI_REG_READ_CMDID',
    0x0015: 'WMI_REG_WRITE_CMDID',
    0x0016: 'WMI_RC_STATE_CHANGE_CMDID',
    0x0017: 'WMI_RC_RATE_UPDATE_CMDID',
    0x0018: 'WMI_TARGET_IC_UPDATE_CMDID',
    0x0019: 'WMI_TX_AGGR_ENABLE_CMDID',
    0x001A: 'WMI_TGT_DETACH_CMDID',
    0x001B: 'WMI_NODE_UPDATE_CMDID',
    0x001C: 'WMI_INT_STATS_CMDID',
    0x001D: 'WMI_TX_STATS_CMDID',
    0x001E: 'WMI_RX_STATS_CMDID',
    0x001F: 'WMI_BITRATE_MASK_CMDID',
    0x0020: 'WMI_REG_RMW_CMDID',
}

WMI_EVENTS = {
    0x1001: 'WMI_TGT_RDY_EVENTID',
    0x1002: 'WMI_SWBA_EVENTID',
    0x1003: 'WMI_FATAL_EVENTID',
    0x1004: 'WMI_TXTO_EVENTID',
    0x1005: 'WMI_BMISS_EVENTID',
    0x1006: 'WMI_DELBA_EVENTID',
    0x1007: 'WMI_TXSTATUS_EVENTID',
    # Note: 0x0013 and 0x0014 are often used as WMI_REG_RSP_EVENTID, etc. 
    # Can be added manually if discovered in dumps.
    0x0013: 'WMI_REG_RSP_EVENTID', 
    0x0014: 'WMI_HWR_MODE_RESP_EVENTID',
}

HTC_MESSAGES = {
    0x0001: 'HTC_MSG_READY_ID',
    0x0002: 'HTC_MSG_CONNECT_SERVICE_ID',
    0x0003: 'HTC_MSG_CONNECT_SERVICE_RESPONSE_ID',
    0x0004: 'HTC_MSG_SETUP_COMPLETE_ID',
    0x0005: 'HTC_MSG_CONFIG_PIPE_ID',
    0x0006: 'HTC_MSG_CONFIG_PIPE_RESPONSE_ID',
}

HTC_SERVICES = {
    0x0100: 'WMI_CONTROL_SVC',
    0x0101: 'WMI_BEACON_SVC',
    0x0102: 'WMI_CAB_SVC',
    0x0103: 'WMI_UAPSD_SVC',
    0x0104: 'WMI_MGMT_SVC',
    0x0105: 'WMI_DATA_VO_SVC',
    0x0106: 'WMI_DATA_VI_SVC',
    0x0107: 'WMI_DATA_BE_SVC',
    0x0108: 'WMI_DATA_BK_SVC',
}

class USBInterceptor:
    """
    Hooks directly into the dev.read and dev.write calls to dump human-readable
    transactions exactly as they occur, bridging the gap between raw bytes and PCAP.
    """
    
    @staticmethod
    def _parse_payload(endpoint: int, data: bytes, direction: str) -> str:
        if not data:
            return "EMPTY"
            
        # Bulk EPs (0x04 and 0x82) have a 4-byte HIF header on top of the HTC packet
        is_bulk = (endpoint & 0x7F) == 0x04 or endpoint == 0x82
        off = 4 if is_bulk else 0
        
        if len(data) < off + 8:
            return f"RAW: {data.hex()}"
            
        # HTC Header is 8 bytes
        htc_raw = data[off:]
        htc_ep = htc_raw[0]
        htc_flags = htc_raw[1]
        htc_len = struct.unpack_from(">H", htc_raw, 2)[0]
        trailer_len = htc_raw[4]
        
        parsed = f"HTC_EP={htc_ep} LEN={htc_len}"
        
        # WMI payload usually starts at offset 8 relative to HTC
        hdr_len = 8
        if len(htc_raw) >= hdr_len + 4:
            wmi_id = struct.unpack_from(">H", htc_raw, hdr_len)[0]
            seq_id = struct.unpack_from(">H", htc_raw, hdr_len + 2)[0]
            
            if htc_ep == 0:
                name = HTC_MESSAGES.get(wmi_id, f"UNKNOWN_HTC_0x{wmi_id:04X}")
                parsed += f" | {name}"
                if wmi_id == 0x0002 and len(htc_raw) >= hdr_len + 8:
                    svc_id = struct.unpack_from(">H", htc_raw, hdr_len + 2)[0]
                    svc_name = HTC_SERVICES.get(svc_id, f"0x{svc_id:04X}")
                    parsed += f" [SVC={svc_name}]"
                elif wmi_id == 0x0003 and len(htc_raw) >= hdr_len + 6:
                    status = htc_raw[hdr_len + 4]
                    epid = htc_raw[hdr_len + 5]
                    parsed += f" [Status={status} Assigned EP={epid}]"
            else:
                if direction == "OUT":
                    name = WMI_COMMANDS.get(wmi_id, f"UNKNOWN_WMI_CMD_0x{wmi_id:04X}")
                else:
                    name = WMI_EVENTS.get(wmi_id, WMI_COMMANDS.get(wmi_id, f"UNKNOWN_WMI_EVT_0x{wmi_id:04X}"))
                parsed += f" | {name} SEQ={seq_id}"
                    
        return f"{parsed} | RAW={data.hex()}"

    @staticmethod
    def log_tx(endpoint: int, data: bytes):
        ts = time.time()
        desc = USBInterceptor._parse_payload(endpoint, data, "OUT")
        logger.info(f"[{ts:.3f}] HOST->DEV | EP=0x{endpoint:02X} | {desc}")

    @staticmethod
    def log_rx(endpoint: int, data: bytes):
        ts = time.time()
        desc = USBInterceptor._parse_payload(endpoint, data, "IN")
        logger.info(f"[{ts:.3f}] DEV->HOST | EP=0x{endpoint:02X} | {desc}")
