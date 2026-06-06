"""Replay-diff engine for rtw88-FAMILY USB ports (the acceptance-gate core).

Reconstructs the exact ordered USB conversation a Realtek rtw88-family driver had
with the chip from a cold-boot capture (control reads/writes + bulk-OUT firmware
packets), then lets a port drive its bring-up against a transport that *replays
the chip's recorded read responses*. Because read-modify-writes see the real chip
values, the port must emit byte-identical writes and bulk packets or a divergence
is raised at the first mismatching op — verifying a milestone with NO hardware.

FAMILY-SPECIFIC: vendor register access is bRequest 0x05, the Realtek rtw88 USB
convention (RTL8814AU / RTL8821AU / RTL8812AU / RTL8822BU, both mainline-derived
and DKMS/PHYDM ports). It is NOT the convention for MT76 (MediaTek), ath9k_htc
(Atheros), or rt2x00 (Ralink RT2800/RT3070) — those ports must not reach for this
module. The bRequest, the submit/completion data-field layout, and the 0x40/0xC0
write/read direction split are all rtw88-USB-specific.

A chip's ``verify_pcap.py`` imports ``extract_ops``, ``ReplayTransport``,
``Divergence`` (and ``frame_epochs`` for per-channel window slicing) and supplies
its own capture dir, device address, frame window, start register, and the
per-milestone bring-up call sequence. Shared so every rtw88 port reuses one
engine instead of re-deriving the tshark extraction.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

RTW_VENDOR_REQ = "5"  # bRequest 0x05 — rtw88-family vendor register access


def _hex(s: str) -> bytes:
    s = s.replace(":", "").strip()
    return bytes.fromhex(s) if s else b""


def find_card_device(pcap: Path) -> int:
    """Device issuing the most Realtek vendor (bRequest 0x05) register transfers == the
    card. Auto-detected so a recipe never pins to a per-capture device number. Filters to
    vendor reads/writes (0x40/0xC0) so standard control traffic can't be mistaken for it."""
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y",
         "(usb.bmRequestType==0x40 || usb.bmRequestType==0xc0) && usb.setup.bRequest==5",
         "-T", "fields", "-e", "usb.device_address"],
        capture_output=True, text=True, check=True).stdout
    counts = Counter(line.strip() for line in out.splitlines() if line.strip())
    if not counts:
        raise SystemExit(f"no Realtek vendor (bRequest 0x05) traffic in {pcap.name}")
    return int(counts.most_common(1)[0][0])


def frame_epochs(pcap: Path):
    """(frame_numbers, epochs) across the whole pcap, monotonic — for the
    epoch->frame bisect that per-channel slicing keys off iw.log timestamps."""
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-T", "fields",
         "-e", "frame.number", "-e", "frame.time_epoch"],
        capture_output=True, text=True, check=True).stdout
    nums, eps = [], []
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2:
            try:
                nums.append(int(p[0]))
                eps.append(float(p[1]))
            except ValueError:
                pass
    return nums, eps


def extract_ops(pcap: Path, dev: int, window, start_addr=None):
    """Ordered list of {'kind','addr','width','value'|'data','frame'} ops within
    ``window`` (an inclusive (first_frame, last_frame) range).

    The synchronous driver issues one transfer at a time, so submit ('S') and its
    completion ('C') are adjacent rows (the urb_id is reused, so it can't pair
    them). Write data is on the submit (``usb.data_fragment``); read data is on the
    completion (``usb.control.Response``); bulk-OUT data is on the submit
    (``usb.capdata``). Vendor register access is bRequest 0x05 (RTW_VENDOR_REQ);
    bmRequestType 0x40 = write (value on the submit), 0xC0 = read (value on the
    completion).

    With ``start_addr`` set, the op list is trimmed to begin at the first op
    touching that register (skipping any open-time preamble). Left None, the window
    is returned verbatim — e.g. per-channel tune diffs that must surface a stray
    pre/post step as the first divergence rather than silently skipping it.
    """
    fields = [
        "frame.number", "usb.urb_type", "usb.transfer_type", "usb.setup.bRequest",
        "usb.bmRequestType", "usb.setup.wValue", "usb.setup.wLength",
        "usb.data_fragment", "usb.capdata", "usb.control.Response",
    ]
    cmd = ["tshark", "-r", str(pcap),
           "-Y", (f"usb.device_address=={dev} && frame.number>={window[0]} "
                  f"&& frame.number<={window[1]} && "
                  "(usb.transfer_type==0x02 || "
                  "(usb.transfer_type==0x03 && usb.endpoint_address.direction==0))"),
           "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    rows = []
    for line in out.splitlines():
        c = line.split("\t")
        c += [""] * (len(fields) - len(c))
        frame, utype, ttype, breq, brt, wval, wlen, dfrag, cap, resp = c[:10]
        rows.append({
            "frame": int(frame), "utype": utype.strip("'"), "ttype": int(ttype, 0),
            "breq": breq, "brt": brt, "wval": wval, "wlen": wlen,
            "dfrag": _hex(dfrag), "cap": _hex(cap), "resp": _hex(resp),
        })

    ops = []
    pending = None  # a control read 'S' awaiting its completion
    for r in rows:
        if r["utype"] == "S":
            if r["ttype"] == 3:  # bulk OUT
                ops.append({"kind": "B", "data": r["cap"] or r["dfrag"], "frame": r["frame"]})
            elif r["ttype"] == 2 and r["breq"] == RTW_VENDOR_REQ:
                addr, width, brt = int(r["wval"], 0), int(r["wlen"], 0), int(r["brt"], 0)
                if brt == 0x40:  # write — value on the submit
                    ops.append({"kind": "W", "addr": addr, "width": width,
                                "value": int.from_bytes(r["dfrag"], "little"),
                                "frame": r["frame"]})
                elif brt == 0xC0:  # read — value arrives on the completion
                    pending = {"addr": addr, "width": width, "frame": r["frame"]}
        elif r["utype"] == "C" and r["ttype"] == 2 and pending is not None:
            # Only a control completion carries the read value; bulk completions
            # (ttype 3) interleave here and must not be mistaken for it.
            ops.append({"kind": "R", "addr": pending["addr"], "width": pending["width"],
                        "value": int.from_bytes(r["resp"], "little"), "frame": pending["frame"]})
            pending = None

    if start_addr is None:
        return ops
    for i, o in enumerate(ops):
        if o.get("addr") == start_addr:
            return ops[i:]
    raise SystemExit(f"start register 0x{start_addr:04x} not found in {pcap.name}")


class Divergence(AssertionError):
    pass


class ReplayTransport:
    """Walks the pcap op list; reads return recorded chip values, writes/bulk are
    checked against the wire. Any mismatch raises Divergence with the diverging op.

    Drop-in for a chip transport's read8/16/32 + write8/16/32 + bulk_out surface,
    so the port's bring-up functions run unchanged against the replay."""

    def __init__(self, ops):
        self.ops = ops
        self.i = 0

    def _next(self, want):
        if self.i >= len(self.ops):
            raise Divergence(f"port emitted extra {want} past end of capture")
        op = self.ops[self.i]
        self.i += 1
        return op

    def _read(self, addr, width):
        op = self._next(f"read(0x{addr:04x}/{width})")
        if op["kind"] != "R" or op["addr"] != addr or op["width"] != width:
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: port read "
                f"0x{addr:04x}/{width}, capture has {self._fmt(op)}")
        return op["value"]

    def _write(self, addr, width, value):
        op = self._next(f"write(0x{addr:04x}/{width})")
        if (op["kind"] != "W" or op["addr"] != addr or op["width"] != width
                or op["value"] != value):
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: port wrote "
                f"0x{addr:04x}/{width}=0x{value:0{width * 2}x}, "
                f"capture has {self._fmt(op)}")

    @staticmethod
    def _fmt(op):
        if op["kind"] == "B":
            return f"bulk[{len(op['data'])}B]"
        return (f"{op['kind']} 0x{op['addr']:04x}/{op['width']}"
                f"=0x{op['value']:0{op['width'] * 2}x}")

    def read8(self, a):
        return self._read(a, 1)

    def read16(self, a):
        return self._read(a, 2)

    def read32(self, a):
        return self._read(a, 4)

    def write8(self, a, v):
        self._write(a, 1, v & 0xFF)

    def write16(self, a, v):
        self._write(a, 2, v & 0xFFFF)

    def write32(self, a, v):
        self._write(a, 4, v & 0xFFFFFFFF)

    def writeN(self, addr, data):
        """Arbitrary-length control write (e.g. the 196/8-byte FW page chunks).
        Checked byte-for-byte against the recorded wide write at this position."""
        data = bytes(data)
        op = self._next(f"writeN(0x{addr:04x}/{len(data)})")
        if op["kind"] != "W" or op["addr"] != addr or op["width"] != len(data):
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: port wrote "
                f"0x{addr:04x}/{len(data)}B, capture has {self._fmt(op)}")
        cap = op["value"].to_bytes(op["width"], "little")
        if data != cap:
            diff = next((j for j in range(len(data)) if data[j] != cap[j]), len(data))
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: writeN payload differs at "
                f"byte {diff} (0x{addr:04x}, {len(data)}B)")

    def write_block(self, addr, data):
        """Alias for writeN: the rtl8xxxu transports (8188eus / 8187) name the wide
        control write ``write_block`` where rtw88 names it ``writeN`` -- same op on the
        wire. Lets those ports' FW-page uploads replay against this transport unchanged."""
        self.writeN(addr, data)

    def bulk_out(self, data):
        op = self._next(f"bulk[{len(data)}B]")
        if op["kind"] != "B":
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: port sent bulk[{len(data)}B], "
                f"capture has {self._fmt(op)}")
        if bytes(data) != op["data"]:
            n = min(len(data), len(op["data"]))
            diff = next((j for j in range(n) if data[j] != op["data"][j]), n)
            raise Divergence(
                f"op#{self.i - 1} frame {op['frame']}: bulk payload differs at "
                f"byte {diff} (len port={len(data)} cap={len(op['data'])})")
