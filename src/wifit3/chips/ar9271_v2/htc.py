"""HTC (Host-Target Communication) framing + the cold-boot handshake.

Ported from ``htc_hst.c`` / ``htc_drv_init.c``. After firmware boot the target sends an
HTC_READY on the REG_IN pipe; the host then connects each service (WMI control first, then
the data services), configures pipe credits, and signals setup-complete. Every HTC control
message rides ENDPOINT0 out the REG_OUT pipe; the target answers each on REG_IN.

Wire layout of one HTC frame: an 8-byte ``htc_frame_hdr`` (endpoint_id, flags, be16
payload_len, control[4]) followed by the message [SRC] htc_hst.h:59-64 / htc_hst.c:21-38.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from . import constants as C
from .transport import AR9271Transport

logger = logging.getLogger(__name__)


def frame(epid: int, payload: bytes, flags: int = 0) -> bytes:
    """Prepend the htc_frame_hdr — mirrors htc_issue_send [SRC] htc_hst.c:29-33."""
    return struct.pack(">BBH", epid, flags, len(payload)) + b"\x00\x00\x00\x00" + payload


def _conn_svc_msg(service_id: int) -> bytes:
    """htc_conn_svc_msg: msg_id, service_id, con_flags, dl_pipeid, ul_pipeid, meta, pad
    [SRC] htc_hst.h:174-182 + htc_hst.c:275-284. con_flags/meta/pad are always 0 here."""
    ul, dl = C.SERVICE_PIPES[service_id]
    return struct.pack(">HHHBBBB", C.HTC_MSG_CONNECT_SERVICE_ID, service_id, 0, dl, ul, 0, 0)


@dataclass
class HTCState:
    credit_size: int = 0
    credits: int = C.HTC_CREDITS_AR9271
    endpoints: dict[int, int] = field(default_factory=dict)   # service_id -> endpoint_id


def process_ready(t: AR9271Transport, st: HTCState) -> None:
    """Consume the target's HTC_READY (credits + credit_size) [SRC] htc_hst.c:88-101."""
    msg = t.reg_in()
    payload = msg[C.HTC_FRAME_HDR_LEN:]
    message_id, credits, credit_size = struct.unpack_from(">HHH", payload)
    if message_id != C.HTC_MSG_READY_ID:
        raise ValueError(f"expected HTC_READY, got message_id 0x{message_id:04x}")
    st.credit_size = credit_size
    logger.debug("HTC ready: credits=%d credit_size=%d", credits, credit_size)


def connect_service(t: AR9271Transport, st: HTCState, service_id: int) -> int:
    """Send CONNECT_SERVICE on ENDPOINT0, read the response, return the assigned endpoint id
    [SRC] htc_hst.c:241-301 htc_connect_service + htc_process_conn_rsp."""
    t.reg_out(frame(C.ENDPOINT0, _conn_svc_msg(service_id)))
    rsp = t.reg_in()[C.HTC_FRAME_HDR_LEN:]
    msg_id, rsp_svc, status, epid = struct.unpack_from(">HHBB", rsp)
    if msg_id != C.HTC_MSG_CONNECT_SERVICE_RESPONSE_ID or status != C.HTC_SERVICE_SUCCESS:
        raise ValueError(f"connect svc 0x{service_id:04x} failed: msg=0x{msg_id:04x} "
                         f"status={status}")
    if not (C.ENDPOINT0 < epid < C.ENDPOINT_MAX):
        raise ValueError(f"connect svc 0x{service_id:04x}: bad endpoint id {epid}")
    st.endpoints[service_id] = epid
    return epid


def config_pipe_credits(t: AR9271Transport, st: HTCState) -> None:
    """htc_config_pipe_msg: message_id, pipe_id (=WLAN_TX), credits [SRC] htc_hst.c:154-186."""
    msg = struct.pack(">HBB", C.HTC_MSG_CONFIG_PIPE_ID, C.USB_WLAN_TX_PIPE, st.credits)
    t.reg_out(frame(C.ENDPOINT0, msg))
    rsp = t.reg_in()[C.HTC_FRAME_HDR_LEN:]
    (msg_id,) = struct.unpack_from(">H", rsp)
    if msg_id != C.HTC_MSG_CONFIG_PIPE_RESPONSE_ID:
        raise ValueError(f"config-pipe response expected, got 0x{msg_id:04x}")


def setup_complete(t: AR9271Transport) -> None:
    """htc_comp_msg: just the SETUP_COMPLETE message id [SRC] htc_hst.c:192-211. The target's
    acknowledgement is not an ENDPOINT0 control message, so we send and move on."""
    t.reg_out(frame(C.ENDPOINT0, struct.pack(">H", C.HTC_MSG_SETUP_COMPLETE_ID)))


def handshake(t: AR9271Transport) -> HTCState:
    """Full cold-boot HTC bring-up. Order is fixed by ath9k_init_htc_services + htc_init:
    READY -> connect every service -> config credits -> setup complete."""
    st = HTCState()
    process_ready(t, st)
    for service_id in C.SERVICE_CONNECT_ORDER:
        connect_service(t, st, service_id)
    config_pipe_credits(t, st)        # htc_init: credits first ...
    setup_complete(t)                 # ... then setup-complete [SRC] htc_hst.c:230-238
    return st
