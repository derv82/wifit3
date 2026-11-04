"""HTC (Host-Target Communication) framing, REG_IN routing + the cold-boot handshake.

Ported from ``htc_hst.c`` / ``htc_drv_init.c``. After firmware boot the target sends an
HTC_READY on the REG_IN pipe; the host then connects each service (WMI control first, then
the data services), configures pipe credits, and signals setup-complete. Every HTC control
message rides ENDPOINT0 out the REG_OUT pipe. Every REG_IN frame goes through
``HTCTarget.rx_msg``, which routes it by endpoint: ENDPOINT0 to the HTC messages, any other
to that endpoint's rx callback (the WMI control endpoint's is ``WMI.ctrl_rx``).

Wire layout of one HTC frame: an 8-byte ``htc_frame_hdr`` (endpoint_id, flags, be16
payload_len, control[4]) followed by the message [SRC] htc_hst.h:59-64 / htc_hst.c:21-38.
"""
from __future__ import annotations

import logging
import struct
import threading
from typing import TYPE_CHECKING, Callable

from . import constants as C

if TYPE_CHECKING:
    from .transport import AR9271Transport

logger = logging.getLogger(__name__)

HTC_FLAGS_RECV_TRAILER = 1 << 1         # [SRC] htc_hst.h:57
HTC_READY_MSG_LEN = 8                   # [SRC] htc_hst.h:66-72 sizeof(struct htc_ready_msg)
HTC_CONN_SVC_RSPMSG_LEN = 8             # [SRC] htc_hst.h:191-199 sizeof(struct htc_conn_svc_rspmsg)
HTC_PANIC_EPID = 0x99                   # [SRC] htc_hst.c:429
HTC_WATCHDOG_PATTERN = 0x00C60000       # [SRC] htc_hst.c:448
HTC_WAIT_TIMEOUT = 1.0                  # HZ [SRC] htc_drv_init.c:89, htc_hst.c:290


class HTCReadyError(Exception):
    """The target sent no HTC_READY after firmware boot [SRC] htc_drv_init.c:79-98."""

    def __init__(self) -> None:
        super().__init__("ath9k_htc: Target is unresponsive (no HTC_READY)")


def frame(epid: int, payload: bytes, flags: int = 0) -> bytes:
    """Prepend the htc_frame_hdr — mirrors htc_issue_send [SRC] htc_hst.c:29-33."""
    return struct.pack(">BBH", epid, flags, len(payload)) + b"\x00\x00\x00\x00" + payload


def _conn_svc_msg(service_id: int) -> bytes:
    """htc_conn_svc_msg: msg_id, service_id, con_flags, dl_pipeid, ul_pipeid, meta, pad
    [SRC] htc_hst.h:174-182 + htc_hst.c:275-284. con_flags/meta/pad are always 0 here."""
    ul, dl = C.SERVICE_PIPES[service_id]
    return struct.pack(">HHHBBBB", C.HTC_MSG_CONNECT_SERVICE_ID, service_id, 0, dl, ul, 0, 0)


class HTCTarget:
    """``struct htc_target`` [SRC] htc_hst.h:116-140. ``endpoints`` maps service_id -> endpoint_id,
    ENDPOINT_UNUSED while its connect is pending; ``endpoint_rx`` is each endpoint's ep_callbacks.rx."""

    def __init__(self) -> None:
        self.credit_size = 0
        self.credits = C.HTC_CREDITS_AR9271
        self.endpoints: dict[int, int] = {}
        self.endpoint_rx: dict[int, Callable[[bytes], None]] = {}
        self.conn_rsp_epid = C.ENDPOINT_UNUSED
        self.target_wait = threading.Event()
        self.cmd_wait = threading.Event()

    def rx_msg(self, msg: bytes) -> None:
        """ath9k_htc_rx_msg [SRC] htc_hst.c:404-492 for a REG_IN frame."""
        length = len(msg)
        if length < C.HTC_FRAME_HDR_LEN:
            return
        epid, flags = msg[0], msg[1]
        if epid == HTC_PANIC_EPID:
            logger.error("ath: firmware panic! %s", msg.hex())
            return
        if epid >= C.ENDPOINT_MAX:
            return
        if epid == C.ENDPOINT0:
            hdr = 0
            if flags & HTC_FLAGS_RECV_TRAILER and struct.unpack_from(">I", msg)[0] == HTC_WATCHDOG_PATTERN:
                hdr = 4
                length -= 4
            if length < C.HTC_FRAME_HDR_LEN + 2:
                return
            msg_id = struct.unpack_from(">H", msg, hdr + C.HTC_FRAME_HDR_LEN)[0]
            if msg_id == C.HTC_MSG_READY_ID:
                if length < HTC_READY_MSG_LEN:
                    return
                self.process_target_rdy(msg[hdr:])
            elif msg_id == C.HTC_MSG_CONNECT_SERVICE_RESPONSE_ID:
                if length < C.HTC_FRAME_HDR_LEN + HTC_CONN_SVC_RSPMSG_LEN:
                    return
                self.process_conn_rsp(msg[hdr + C.HTC_FRAME_HDR_LEN:])
            return
        if flags & HTC_FLAGS_RECV_TRAILER:
            msg = msg[:length - msg[4]]
        rx = self.endpoint_rx.get(epid)
        if rx is not None:
            rx(msg[C.HTC_FRAME_HDR_LEN:])

    def process_target_rdy(self, htc_hdr: bytes) -> None:
        """htc_process_target_rdy [SRC] htc_hst.c:88-101; rx_msg hands it the frame header, not the
        message (htc_hst.c:466), so credit_size reads control[0..1]."""
        self.credit_size = struct.unpack_from(">H", htc_hdr, 4)[0]
        logger.debug("HTC ready: credit_size=%d", self.credit_size)
        self.target_wait.set()

    def process_conn_rsp(self, svc_rspmsg: bytes) -> None:
        """htc_process_conn_rsp [SRC] htc_hst.c:103-152."""
        _, service_id, status, epid = struct.unpack_from(">HHBB", svc_rspmsg)
        if status != C.HTC_SERVICE_SUCCESS:
            self.conn_rsp_epid = C.ENDPOINT_UNUSED
            return
        if epid <= C.ENDPOINT0 or epid >= C.ENDPOINT_MAX:
            return
        if service_id not in self.endpoints:
            return
        self.endpoints[service_id] = epid
        self.conn_rsp_epid = epid
        self.cmd_wait.set()


def process_ready(t: AR9271Transport) -> None:
    """ath9k_htc_wait_for_target [SRC] htc_drv_init.c:79-98."""
    if not t.wait_for_completion_timeout(t.htc.target_wait, HTC_WAIT_TIMEOUT):
        raise HTCReadyError()


def connect_service(t: AR9271Transport, service_id: int) -> int:
    """htc_connect_service [SRC] htc_hst.c:241-305: send CONNECT_SERVICE on ENDPOINT0 and wait for
    htc_process_conn_rsp to assign the endpoint id."""
    target = t.htc
    target.endpoints[service_id] = C.ENDPOINT_UNUSED
    target.cmd_wait.clear()
    t.reg_out(frame(C.ENDPOINT0, _conn_svc_msg(service_id)))
    if not t.wait_for_completion_timeout(target.cmd_wait, HTC_WAIT_TIMEOUT):
        raise TimeoutError(f"Service connection timeout for: 0x{service_id:04x}")
    if target.conn_rsp_epid < 0 or target.conn_rsp_epid >= C.ENDPOINT_MAX:
        raise ValueError(f"connect svc 0x{service_id:04x}: bad endpoint id {target.conn_rsp_epid}")
    return target.conn_rsp_epid


def config_pipe_credits(t: AR9271Transport) -> None:
    """htc_config_pipe_msg: message_id, pipe_id (=WLAN_TX), credits [SRC] htc_hst.c:154-190.
    Linux waits only on the OUT's completion (:342-346); rx_msg drops the response."""
    msg = struct.pack(">HBB", C.HTC_MSG_CONFIG_PIPE_ID, C.USB_WLAN_TX_PIPE, t.htc.credits)
    t.reg_out(frame(C.ENDPOINT0, msg))


def setup_complete(t: AR9271Transport) -> None:
    """htc_comp_msg: just the SETUP_COMPLETE message id [SRC] htc_hst.c:192-226; Linux waits only
    on the OUT's completion (:348-352)."""
    t.reg_out(frame(C.ENDPOINT0, struct.pack(">H", C.HTC_MSG_SETUP_COMPLETE_ID)))


def handshake(t: AR9271Transport) -> HTCTarget:
    """Full cold-boot HTC bring-up. Order is fixed by ath9k_init_htc_services + htc_init:
    READY -> connect every service -> config credits -> setup complete."""
    process_ready(t)
    for service_id in C.SERVICE_CONNECT_ORDER:
        connect_service(t, service_id)
    config_pipe_credits(t)            # htc_init: credits first ...
    setup_complete(t)                 # ... then setup-complete [SRC] htc_hst.c:230-238
    return t.htc
