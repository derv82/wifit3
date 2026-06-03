"""Acceptance gate: replay-diff the port against the cold-boot capture.

Reconstructs the exact ordered USB conversation the vendor driver had with the
chip (control reads/writes + bulk-OUT FW packets), then drives the port's
implemented bring-up against a transport that *replays the chip's recorded read
responses*. Because read-modify-writes see the real chip values, the port must
emit byte-identical writes and bulk packets.

Coverage grows with the port — currently M1 (power-on -> firmware -> FW-ready,
incl. all 46 FW packets, which verifies the blob) plus M2a (the MAC register
table). PASS = the port reproduces the capture's USB traffic from _InitPowerOn
through the latest implemented milestone, byte-for-byte.

Run: ``uv run python scripts/rtl8814au_dkms/verify_pcap.py [capture-N.pcap]``
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8814au_dkms import bb, firmware, mac  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"

# Card device address per capture (lsusb devnum); FW download lives in the
# airmon/open phase, which starts at frame 5707 in every capture.
DEV_ADDR = {"capture-1": 51, "capture-2": 53, "capture-3": 54}
WINDOW = (5707, 14000)  # M1 + M2a + M2b MISC/BB (PHY_REG+AGC_TAB end ~frame 11318)
START_ADDR = 0x10C2  # first register _InitPowerOn_8814AU touches


def _hex(s: str) -> bytes:
    s = s.replace(":", "").strip()
    return bytes.fromhex(s) if s else b""


def extract_ops(pcap: Path, dev: int):
    """Ordered list of {'kind','addr','width','value'|'data','frame'} ops.

    The synchronous driver issues one transfer at a time, so submit ('S') and its
    completion ('C') are adjacent rows (the urb_id is reused, so it can't pair
    them). Write data is on the submit (``usb.data_fragment``); read data is on
    the completion (``usb.control.Response``); bulk-OUT data is on the submit
    (``usb.capdata``). Vendor register access is bRequest 0x05.
    """
    fields = [
        "frame.number", "usb.urb_type", "usb.transfer_type", "usb.setup.bRequest",
        "usb.bmRequestType", "usb.setup.wValue", "usb.setup.wLength",
        "usb.data_fragment", "usb.capdata", "usb.control.Response",
    ]
    cmd = ["tshark", "-r", str(pcap),
           "-Y", (f"usb.device_address=={dev} && frame.number>={WINDOW[0]} "
                  f"&& frame.number<={WINDOW[1]} && "
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
            elif r["ttype"] == 2 and r["breq"] == "5":
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

    # Trim to the _InitPowerOn entry point (skip any open-time preamble).
    for i, o in enumerate(ops):
        if o.get("addr") == START_ADDR:
            return ops[i:]
    raise SystemExit(f"start register 0x{START_ADDR:04x} not found in {pcap.name}")


class Divergence(AssertionError):
    pass


class ReplayTransport:
    """Walks the pcap op list; reads return recorded chip values, writes/bulk are
    checked against the wire. Any mismatch raises with the diverging op."""

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


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "capture-1"
    name = Path(name).stem
    pcap = CAP_DIR / f"{name}.pcap"
    fw = FW_BIN.read_bytes()

    print(f"Extracting USB op stream from {pcap.name} (dev {DEV_ADDR[name]})...")
    ops = extract_ops(pcap, DEV_ADDR[name])
    n_bulk = sum(1 for o in ops if o["kind"] == "B")
    print(f"  {len(ops)} ops in M1 window ({n_bulk} firmware packets)")

    time.sleep = lambda *a, **k: None  # replay needs no real delays
    t = ReplayTransport(ops)
    try:
        ready = firmware.bring_up(t, fw)   # M1: power-on -> FW download -> ready
        if ready:
            mac.phy_mac_config(t)          # M2a: MAC register table
            mac.mac_init_misc(t)           # M2b: hal_init MISC stage
            bb.phy_bb_config(t)            # M2b: PHY_BBConfig8814 (prefix so far)
    except Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    if not ready:
        print("\nFAIL: bring_up did not reach CPU_DL_READY against the capture")
        return 1
    print(f"\nPASS: port reproduced {t.i} USB ops byte-for-byte through M2b "
          f"({n_bulk} FW packets / {len(fw)} B blob; MAC table + MISC stage + "
          f"PHY_BBConfig8814: prefix, PHY_REG + AGC_TAB tables, crystal-cap, TRX path).")
    print(f"      {len(ops) - t.i} later-milestone ops remain in the capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
