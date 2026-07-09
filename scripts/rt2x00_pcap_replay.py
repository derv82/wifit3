"""Replay-diff engine for rt2x00-FAMILY USB ports (Ralink RT2500USB / RT2800USB).

Reconstructs the ordered USB conversation a Ralink rt2x00 driver had with the chip from a
cold-boot capture, then lets a port drive its bring-up against a transport that replays the
chip's recorded reads -- so read-modify-writes see the real values and the port must emit
byte-identical writes or a Divergence is raised at the first mismatch. No hardware.

FAMILY-SPECIFIC wire format (rt2x00usb, NOT Realtek's bRequest 0x05 / addr-in-wValue):

    bRequest 6 = USB_MULTI_WRITE, 7 = USB_MULTI_READ  -- the register access
    bRequest 9 = USB_EEPROM_READ (one-shot, wValue=wIndex=0)
    bRequest 1 = USB_DEVICE_MODE, 2 = USB_SINGLE_WRITE -- no data; value rides in wValue
    bmRequestType 0x40 = OUT/write (data on the submit), 0xC0 = IN/read (data on completion)
    register ADDRESS goes in wIndex (wValue is 0 for register access)

Unlike the Realtek ReplayTransport (which reimplements read8/write8/...), this replays at
the ctrl_transfer layer: ``ReplayDevice`` is a fake usb.core.Device whose ``ctrl_transfer``
walks the recorded ops. The REAL chip transport (RT2500USBTransport / RT2800USBTransport)
then drives it unchanged, so every helper -- regbusy_read, set_state's poll loop,
write*_mask, read_eeprom, the single-writes -- replays with zero reimplementation.

A chip's ``verify_pcap.py`` imports ``find_card_device``, ``extract_ops``,
``audit_coverage``, ``ReplayDevice`` and ``Divergence``, then constructs the real transport
around a ReplayDevice and drives its per-milestone bring-up call sequence.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

REQ_MULTI_WRITE = 6
REQ_MULTI_READ = 7
REQ_EEPROM_READ = 9


def _hex(s: str) -> bytes:
    s = s.replace(":", "").strip()
    return bytes.fromhex(s) if s else b""


def _tshark(pcap: Path, display_filter: str, fields: list[str]) -> list[list[str]]:
    cmd = ["tshark", "-r", str(pcap), "-Y", display_filter, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        c = line.split("\t")
        c += [""] * (len(fields) - len(c))
        rows.append(c)
    return rows


def find_card_device(pcap: Path) -> int:
    """Device issuing the most rt2x00 register transfers (bRequest 6/7) == the card.
    Auto-detected so the harness never pins to a per-capture device number."""
    rows = _tshark(pcap,
                   "(usb.bmRequestType==0x40 || usb.bmRequestType==0xc0) && "
                   "(usb.setup.bRequest==6 || usb.setup.bRequest==7)",
                   ["usb.device_address"])
    counts = Counter(r[0] for r in rows if r and r[0])
    if not counts:
        raise SystemExit(f"no rt2x00 (bRequest 6/7) traffic in {pcap.name}")
    return int(counts.most_common(1)[0][0])


_TT = {"0x00": "iso", "0x01": "interrupt", "0x02": "control", "0x03": "bulk"}


def audit_coverage(pcap: Path, dev: int) -> bool:
    """Confront ALL of the card's traffic so the replay can't be silently blind to a
    channel. Control (vendor regs + EEPROM + FW chunks) is what we reproduce; bulk-IN is
    RX input we ignore; anything else is a blind spot worth investigating before a PASS."""
    rows = _tshark(pcap, f"usb.device_address=={dev}",
                   ["usb.transfer_type", "usb.endpoint_address"])
    seen: Counter = Counter()
    for r in rows:
        ttype = r[0] if r else ""
        ep = r[1] if len(r) > 1 else ""
        if ttype:
            seen[(ttype, ep)] += 1
    print(f"  coverage audit (dev{dev}):")
    ok = True
    for (ttype, ep), n in sorted(seen.items()):
        name = _TT.get(ttype, f"type{ttype}")
        is_in = bool(ep) and (int(ep, 16) & 0x80)
        if ttype == "0x02":
            tag = "REPRODUCE  (control: vendor regs + EEPROM + FW chunks)"
        elif ttype == "0x03" and is_in:
            tag = "input only (bulk-IN RX, chip->host)"
        elif ttype == "0x03" and not is_in:
            tag = "out of scope (bulk-OUT TX frames; the bring-up gate is control-only)"
        else:
            tag = f"** BLIND SPOT: {name} {'IN' if is_in else 'OUT'} **"
            ok = False
        print(f"     {name:9} ep {ep or '--':>4}  {n:>7}  {tag}")
    return ok


def extract_ops(pcap: Path, dev: int, window=None, start=None) -> list[dict]:
    """Ordered control ops for the card. Each op is one vendor ctrl_transfer dict:
    ``{dir, breq, wval, addr (=wIndex), width (=wLength), data, frame}``.

    The synchronous driver issues one transfer at a time, so submit ('S') and its
    completion ('C') are adjacent. Write data is on the submit (``usb.data_fragment``);
    read data is on the completion (``usb.control.Response``). No-data single ops carry
    their value in ``wval`` and have ``width`` 0.

    ``window`` = inclusive (first_frame, last_frame). ``start`` = a predicate
    ``op -> bool``; the stream is trimmed to begin at the first matching op (skipping any
    enumeration/preamble before the milestone of interest).
    """
    fields = ["frame.number", "usb.urb_type", "usb.transfer_type", "usb.bmRequestType",
              "usb.setup.bRequest", "usb.setup.wValue", "usb.setup.wIndex",
              "usb.setup.wLength", "usb.data_fragment", "usb.control.Response"]
    # Keep the query unfiltered on bmRequestType: a read's COMPLETION row carries no
    # bmRequestType, so filtering it in the query would drop read values and break
    # submit/completion pairing. Standard requests (GET_DESCRIPTOR is bRequest 6, same
    # number as USB_MULTI_WRITE, but bmRequestType 0x80) are rejected in the loop instead.
    flt = f"usb.device_address=={dev} && usb.transfer_type==0x02"
    if window:
        flt += f" && frame.number>={window[0]} && frame.number<={window[1]}"

    ops: list[dict] = []
    pending = None  # an IN (read) submit awaiting its completion
    for c in _tshark(pcap, flt, fields):
        frame, utype, _ttype, brt, breq, wval, widx, wlen, dfrag, resp = c[:10]
        utype = utype.strip("'")
        if utype == "S":
            brt_i = int(brt, 0) if brt else -1
            if brt_i not in (0x40, 0xC0):         # not a vendor request -- skip standard
                continue                          # (GET_DESCRIPTOR is bRequest 6 too)
            op = {"breq": int(breq, 0) if breq else -1,
                  "wval": int(wval, 0) if wval else 0,
                  "addr": int(widx, 0) if widx else 0,
                  "width": int(wlen, 0) if wlen else 0,
                  "frame": int(frame)}
            if brt_i == 0xC0:                     # IN / read -- value on the completion
                op["dir"] = "IN"
                pending = op
            else:                                # OUT / write -- data on the submit
                op["dir"] = "OUT"
                op["data"] = _hex(dfrag)
                ops.append(op)
        elif utype == "C" and pending is not None:
            pending["data"] = _hex(resp)
            ops.append(pending)
            pending = None

    if start is not None:
        for i, o in enumerate(ops):
            if start(o):
                return ops[i:]
        raise SystemExit(f"start anchor not found in {pcap.name}")
    return ops


def extract_bulk_out(pcap: Path, dev: int) -> list[dict]:
    """Ordered bulk-OUT submissions (host->chip TX frames) for the card. Each op is
    ``{ep, data (bytes), frame}`` -- the TXINFO+TXWI+802.11+pad payload the driver
    wrote to an AC_* bulk-OUT endpoint (aireplay-ng / airodump inject). Unlike the
    register conversation these are on their own endpoints and carry the full TX
    descriptor, so a TX-fidelity gate can rebuild them from the port's tx.py."""
    rows = _tshark(pcap,
                   f"usb.device_address=={dev} && usb.transfer_type==0x03 && "
                   "usb.endpoint_address<0x80 && usb.capdata",
                   ["frame.number", "usb.endpoint_address", "usb.capdata"])
    out = []
    for frame, ep, data in rows:
        payload = _hex(data)
        if payload:
            out.append({"ep": int(ep, 0), "data": payload, "frame": int(frame)})
    return out


class Divergence(AssertionError):
    pass


class ReplayDevice:
    """A fake usb.core.Device: ``ctrl_transfer`` walks the recorded op stream, returning
    recorded read bytes and byte-checking writes. The real chip transport drives it, so
    the whole transport surface replays unchanged. First mismatch raises Divergence."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0

    def _next(self) -> dict:
        if self.i >= len(self.ops):
            raise Divergence("port issued a transfer past the end of the capture")
        op = self.ops[self.i]
        self.i += 1
        return op

    @staticmethod
    def _fmt(op: dict) -> str:
        d = op.get("data", b"")
        body = (f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d
                else f" wVal=0x{op['wval']:04x}")
        return f"{op['dir']} req{op['breq']} 0x{op['addr']:04x}{body} @f{op['frame']}"

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_wLength, timeout=None):
        op = self._next()
        is_in = bool(bmRequestType & 0x80)
        exp = "IN" if is_in else "OUT"
        if (op["dir"] != exp or op["breq"] != bRequest
                or op["wval"] != wValue or op["addr"] != wIndex):
            want = f"{exp} req{bRequest} 0x{wIndex:04x} wVal=0x{wValue:04x}"
            raise Divergence(f"port issued {want}, capture has {self._fmt(op)}")
        if is_in:
            if op["width"] != data_or_wLength:
                raise Divergence(
                    f"read 0x{wIndex:04x}: port wants {data_or_wLength}B, capture has "
                    f"{op['width']}B @f{op['frame']}")
            return op["data"]                    # bytes; transport does bytes(data)[...]
        payload = bytes(data_or_wLength) if data_or_wLength else b""
        if op["data"] != payload:
            raise Divergence(
                f"write 0x{wIndex:04x}: port {payload.hex() or '(no data)'} != capture "
                f"{op['data'].hex() or '(no data)'} @f{op['frame']}")
        return len(payload)
