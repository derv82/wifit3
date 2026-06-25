"""Replay-diff engine for the AR9271 (ath9k_htc) USB conversation.

STRICT, single-cursor: the port must emit the EXACT host->device op stream the kernel
recorded, in order. The cursor stops at the first op that differs. Modelled on
``scripts/mt76usb_pcap_replay.py`` (same Linux-usbmon pcapng record layout), retargeted to
ath9k's four pipes [SRC] hif_usb.h:69-72 / hif_usb.c:1367-1370:

    EP 0x00  control      cold-boot firmware download (bRequest 0x30 / 0x31), host->device
    EP 0x01  bulk  OUT    USB_WLAN_TX_PIPE — HTC/TX frames        (host->device, positional)
    EP 0x04  int   OUT    USB_REG_OUT_PIPE — WMI/HTC commands     (host->device, positional)
    EP 0x82  bulk  IN     USB_WLAN_RX_PIPE — HIF RX stream        (device->host, fed back)
    EP 0x83  int   IN     USB_REG_IN_PIPE  — WMI/HTC ctrl events  (device->host, fed back)

Host ops (control-OUT, bulk-OUT 0x01, int-OUT 0x04) are the positional cursor. Device->host
data (bulk-IN 0x82, int-IN 0x83) is the response stream the port consumes, not output we
verify. A divergence at op #1 is an honest result: the port did something the kernel didn't.

The AR9271 re-enumerates after the firmware "complete" write; on the Linux capture it keeps
its USB device address across that boundary, so one device-address demux spans cold + warm.
"""
from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

# usbmon mon_bin record offsets — identical layout to the mt76 captures ([WIRE]).
_OFF_TYPE, _OFF_XFER, _OFF_EP, _OFF_DEV = 8, 9, 10, 11
_OFF_LENCAP = 36
_OFF_SETUP = 40        # bmReq@40 bReq@41 wValue@42 wIndex@44 wLength@46
_OFF_DATA = 64

_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_XFER_CTRL, _XFER_INT, _XFER_BULK = 0x02, 0x01, 0x03

# ath9k endpoint addresses (number | direction bit).
EP_WLAN_TX = 0x01      # bulk OUT
EP_REG_OUT = 0x04      # int  OUT
EP_WLAN_RX = 0x82      # bulk IN
EP_REG_IN = 0x83       # int  IN

EP_OUT_HOST = {EP_WLAN_TX, EP_REG_OUT}
EP_IN_DEV = {EP_WLAN_RX, EP_REG_IN}

# Cold-boot firmware-download bRequests [SRC] hif_usb.h:47-48.
FW_DOWNLOAD, FW_DOWNLOAD_COMP = 0x30, 0x31


class Divergence(AssertionError):
    """Raised at the first op the port does not reproduce byte-for-byte."""


def parse_pcapng(path: str) -> list[bytes]:
    """Extract Enhanced-Packet-Block payloads (the usbmon records) from a pcapng."""
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:                 # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_card(pkts: list[bytes]) -> int | None:
    """The card is the device issuing the firmware-download control writes (bRequest 0x30)
    — unambiguous for ath9k, and robust to whatever else shares the bus."""
    counts: Counter = Counter()
    for pkt in pkts:
        if len(pkt) < 48 or pkt[_OFF_XFER] != _XFER_CTRL or pkt[_OFF_TYPE] != _URB_SUBMIT:
            continue
        if pkt[_OFF_SETUP + 1] in (FW_DOWNLOAD, FW_DOWNLOAD_COMP):
            counts[pkt[_OFF_DEV]] += 1
    return counts.most_common(1)[0][0] if counts else None


def extract(pkts: list[bytes], dev: int) -> dict:
    """Demux device ``dev`` into the strict cursor's structures.

      host_ops   ordered positional cursor: every control-OUT (FW download) + every
                 bulk-OUT (0x01) + every int-OUT (0x04), in capture order. Each op:
                 ctrl -> {kind:'ctrl', breq, wval, widx, data, frame}
                 bulk/int -> {kind:'bulk'|'int', ep, data, frame}
      responses  ordered device->host payloads (int-IN 0x83 + bulk-IN 0x82), fed back in
                 capture order, tagged by ep so the port can await the right stream.
    """
    host_ops: list[dict] = []
    responses: list[dict] = []
    frame_no = 0

    for pkt in pkts:
        frame_no += 1
        if len(pkt) < 48 or pkt[_OFF_DEV] != dev:
            continue
        utype, xfer, ep = pkt[_OFF_TYPE], pkt[_OFF_XFER], pkt[_OFF_EP]
        lencap = struct.unpack_from("<I", pkt, _OFF_LENCAP)[0]
        data = bytes(pkt[_OFF_DATA:_OFF_DATA + min(lencap, len(pkt) - _OFF_DATA)])

        if xfer == _XFER_CTRL:
            if utype != _URB_SUBMIT:
                continue
            breq = pkt[_OFF_SETUP + 1]
            if breq not in (FW_DOWNLOAD, FW_DOWNLOAD_COMP):
                continue                          # enumeration noise — not a host op we drive
            wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
            payload = bytes(pkt[_OFF_DATA:_OFF_DATA + min(wlen, len(pkt) - _OFF_DATA)])
            host_ops.append({"kind": "ctrl", "breq": breq, "wval": wval, "widx": widx,
                             "data": payload, "frame": frame_no})

        elif xfer in (_XFER_BULK, _XFER_INT):
            kind = "bulk" if xfer == _XFER_BULK else "int"
            if utype == _URB_SUBMIT and ep in EP_OUT_HOST:
                host_ops.append({"kind": kind, "ep": ep, "data": data, "frame": frame_no})
            elif utype == _URB_COMPLETE and ep in EP_IN_DEV and lencap > 0:
                responses.append({"ep": ep, "data": data, "frame": frame_no})

    return {"host_ops": host_ops, "responses": responses}


def fmt_op(op: dict) -> str:
    if op["kind"] == "ctrl":
        return (f"ctrl-WR breq=0x{op['breq']:02x} wValue=0x{op['wval']:04x} "
                f"({len(op['data'])}B) @f{op['frame']}")
    return f"{op['kind']}-OUT ep=0x{op['ep']:02x} ({len(op['data'])}B) @f{op['frame']}"


class ReplayDevice:
    """A fake ``usb.core.Device`` driven by the real chip transport. ctrl_transfer / write
    walk the recorded host-op stream positionally; the first op that differs raises
    Divergence. read() serves device->host responses in capture order (filtered by ep)."""

    def __init__(self, host_ops: list[dict], responses: list[dict] | None = None,
                 op_start: int = 0, resp_pos: dict[int, int] | None = None):
        self.ops = host_ops                       # full host-op list; op_start is the cursor
        self.i = op_start
        # Per-ep response queues: reading the REG_IN (0x83) stream must not consume or
        # discard pending WLAN_RX (0x82) frames, and vice versa. resp_pos carries the
        # progress so a multi-call WMI conversation reads responses continuously.
        self.resp: dict[int, list[bytes]] = {}
        for r in (responses or []):
            self.resp.setdefault(r["ep"], []).append(r["data"])
        self.resp_pos: dict[int, int] = dict(resp_pos) if resp_pos else {}

    def _next(self) -> dict:
        if self.i >= len(self.ops):
            raise Divergence(f"port issued an op past the end of the capture (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_wLength, timeout=None):
        op = self._next()
        if op["kind"] != "ctrl":
            raise Divergence(f"op #{self.i-1}: port ctrl breq=0x{bRequest:02x} "
                             f"wValue=0x{wValue:04x}, wire has {fmt_op(op)}")
        if op["breq"] != bRequest or op["wval"] != wValue or op["widx"] != wIndex:
            raise Divergence(
                f"op #{self.i-1}: port ctrl breq=0x{bRequest:02x} wValue=0x{wValue:04x} "
                f"wIndex=0x{wIndex:04x}, wire has {fmt_op(op)}")
        payload = b"" if isinstance(data_or_wLength, int) else bytes(data_or_wLength)
        if op["data"] != payload:
            n = min(len(op["data"]), len(payload))
            d = next((k for k in range(n) if op["data"][k] != payload[k]), n)
            raise Divergence(
                f"op #{self.i-1}: ctrl wValue=0x{wValue:04x} payload mismatch at byte {d} "
                f"(len {len(payload)} vs wire {len(op['data'])}) @f{op['frame']}")
        return len(payload)

    def write(self, ep, data, timeout=None):
        op = self._next()
        data = bytes(data)
        if op["kind"] not in ("bulk", "int") or op["ep"] != ep:
            raise Divergence(f"op #{self.i-1}: port OUT ep=0x{ep:02x}, wire has {fmt_op(op)}")
        if op["data"] != data:
            n = min(len(op["data"]), len(data))
            d = next((k for k in range(n) if op["data"][k] != data[k]), n)
            pb = data[d:d+4].hex() if d < len(data) else "-"
            wb = op["data"][d:d+4].hex() if d < len(op["data"]) else "-"
            raise Divergence(
                f"op #{self.i-1}: OUT ep=0x{ep:02x} mismatch at byte {d} — port {pb} vs "
                f"wire {wb} (len {len(data)} vs {len(op['data'])}) @f{op['frame']}")
        return len(data)

    def read(self, ep, length, timeout=None):
        pos = self.resp_pos.get(ep, 0)
        queue = self.resp.get(ep, [])
        if pos >= len(queue):
            raise Divergence(f"port awaited an IN ep=0x{ep:02x} response the wire never carried")
        self.resp_pos[ep] = pos + 1
        return bytearray(queue[pos])
