"""RTL8188FTV DKMS C2H hidden-report handshake (M2 tail, wire only).

Ported from ``hal_read_mac_hidden_rpt`` (hal/hal_com.c:954-1010): request the
report, poll for it, read the bytes, release. The ``c2h_mac_hidden_rpt[_2]_hdl``
field decodes populate ``hal_spec`` (display data consumed by nothing in the
port yet) and are deferred; the raw report bytes are returned for the record.
"""
from __future__ import annotations

import time

from . import constants as C


def request_hidden_report(t) -> None:
    t.write8(C.REG_C2HEVT_MSG_NORMAL, C.C2H_DEFEATURE_RSVD)


def collect_hidden_report(t, timeout_ms: int = 800, min_cnt: int = 10) -> tuple[int, bytes]:
    start = time.monotonic()
    cnt = 0
    ident = 0
    while True:
        cnt += 1
        ident = t.read8(C.REG_C2HEVT_MSG_NORMAL)
        if ident == C.C2H_MAC_HIDDEN_RPT:
            break
        time.sleep(0.010)
        if not ((time.monotonic() - start) * 1000 < timeout_ms or cnt < min_cnt):
            break
    report = bytearray()
    if ident == C.C2H_MAC_HIDDEN_RPT:
        for i in range(C.MAC_HIDDEN_RPT_LEN + C.MAC_HIDDEN_RPT_2_LEN):
            report.append(t.read8(C.REG_C2HEVT_MSG_NORMAL + 2 + i))
    t.write8(C.REG_C2HEVT_MSG_NORMAL, C.C2H_DBG)
    return ident, bytes(report)
