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

from wifit3.chips.rtl8814au_dkms import bb, chan, dm, efuse, firmware, mac, monitor, rf  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"

# Card device address per capture (lsusb devnum); FW download lives in the
# airmon/open phase, which starts at frame 5707 in every capture.
DEV_ADDR = {"capture-1": 51, "capture-2": 53, "capture-3": 54}
WINDOW = (5707, 30000)  # M1 + M2a + M2b (BB ~11318) + M2c (RF radio tables)
START_ADDR = 0x10C2  # first register _InitPowerOn_8814AU touches


def _hex(s: str) -> bytes:
    s = s.replace(":", "").strip()
    return bytes.fromhex(s) if s else b""


def extract_ops(pcap: Path, dev: int, trim_to_start: bool = True):
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

    if not trim_to_start:
        return ops          # caller wants the window verbatim (e.g. per-channel tune)
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


def _read_efuse_params(pcap, dev):
    """Replay the probe-phase efuse read to recover the real chip params.

    The init (M1+) window starts at _InitPowerOn, so the efuse read precedes it.
    Replaying it here means M2b+ consumes the actual efuse-decoded rfe_type /
    crystal_cap / tx_power instead of hardcoded values (the read itself is checked
    byte-for-byte by verify_efuse_pcap.py)."""
    global WINDOW, START_ADDR
    save = (WINDOW, START_ADDR)
    WINDOW, START_ADDR = (1, 7000), 0x00F0
    try:
        t = ReplayTransport(extract_ops(pcap, dev))
        return efuse.read_chip_params(t)
    finally:
        WINDOW, START_ADDR = save


def verify_monitor_block(ops) -> tuple:
    """Targeted diff of the monitor opmode entry (M3b-2).

    wifit3 enters monitor directly, so it does NOT replay airmon's STA->monitor
    dance that the cold-boot pcap shows between the hal_init turn-on tail (M3b-1)
    and the actual monitor opmode entry. The contiguous differ therefore stops at
    M3b-1; the monitor entry is verified here as a standalone 10-op block.

    Anchor on the single monitor RCR write (W REG_RCR=RCR_MONITOR_VALUE); the block
    is the 6 reads (Set_MSR read + RCR/RXFLTMAP0/1/2 backups) before it, that write,
    and the 3 RXFLTMAP writes after it. Replaying monitor.enter_monitor against just
    those ops proves the port emits them byte-for-byte.
    """
    from wifit3.chips.rtl8814au_dkms import constants as C
    k = next((i for i, o in enumerate(ops)
              if o["kind"] == "W" and o["addr"] == C.REG_RCR
              and o["value"] == C.RCR_MONITOR_VALUE), None)
    if k is None:
        raise Divergence("monitor RCR write (0x608=0x90003b2f) not found in capture")
    block = ops[k - 6:k + 4]            # Set_MSR(2) + 4 backups + RCR + 3 RXFLTMAP
    t = ReplayTransport(block)
    monitor.enter_monitor(t)
    if t.i != len(block):
        raise Divergence(f"monitor block: port emitted {t.i} of {len(block)} ops")
    return block[0]["frame"], block[-1]["frame"], len(block)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "capture-1"
    name = Path(name).stem
    pcap = CAP_DIR / f"{name}.pcap"
    fw = FW_BIN.read_bytes()
    time.sleep = lambda *a, **k: None  # replay needs no real delays

    p = _read_efuse_params(pcap, DEV_ADDR[name])
    print(f"Efuse params: rfe_type={p.rfe_type} crystal_cap=0x{p.crystal_cap:02x}")

    print(f"Extracting USB op stream from {pcap.name} (dev {DEV_ADDR[name]})...")
    ops = extract_ops(pcap, DEV_ADDR[name])
    n_bulk = sum(1 for o in ops if o["kind"] == "B")
    print(f"  {len(ops)} ops in M1 window ({n_bulk} firmware packets)")

    t = ReplayTransport(ops)
    try:
        ready = firmware.bring_up(t, fw)   # M1: power-on -> FW download -> ready
        if ready:
            mac.phy_mac_config(t)          # M2a: MAC register table
            mac.mac_init_misc(t)           # M2b: hal_init MISC stage
            bb.phy_bb_config(t, p.rfe_type, p.crystal_cap)  # M2b: PHY_BBConfig8814
            rf.phy_rf_config(t, p.rfe_type)                 # M2c: PHY_RFConfig8814A
            # The 5G BB-swing is unused on the 2.4 GHz init tune (the band switch to 5G
            # only happens on a runtime 5G hop, M5c); passed for the signature.
            chan.init_tune(t, 1, p.tx_power, p.bb_swing, p.bb_swing_5g)  # M2d ch tune + M2e TX power
            dm.init_hal_dm(t)                               # M3a: MISC11 + InitHalDm seed
            chan.set_rfe_reg_init(t, p.rfe_type)            # M3b-1: PHY_SetRFEReg8814A(TRUE)
            mac.hal_init_turn_on(t, p.mac_address)          # M3b-1: turn-on tail + MAC addr
        contiguous = t.i
        # M3b-2 (monitor opmode entry) is verified out-of-line: wifit3 enters
        # monitor directly and skips airmon's STA->monitor dance, so it is not
        # contiguous with M3b-1 on the wire. See verify_monitor_block.
        mon = verify_monitor_block(ops) if ready else None
    except Divergence as e:
        print(f"\nFAIL (divergence): {e}")
        return 1

    if not ready:
        print("\nFAIL: bring_up did not reach CPU_DL_READY against the capture")
        return 1
    print(f"\nPASS: port reproduced {contiguous} USB ops byte-for-byte through M3b-1 "
          f"({n_bulk} FW packets / {len(fw)} B blob; MAC + MISC + BB + RF + channel "
          f"tune + TX power + InitHalDm phydm seed + hal_init turn-on tail "
          f"(RFE-true, NAV, MAC addr), ch1 @ 20 MHz).")
    print(f"      M3b-2 monitor opmode entry verified byte-for-byte as a {mon[2]}-op "
          f"block (Set_MSR(NOLINK) + RCR/RXFLTMAP accept-all) at frames "
          f"{mon[0]}-{mon[1]}; airmon's STA->monitor ops in between are intentionally "
          f"not replayed (wifit3 is always-monitor).")
    print(f"      {len(ops) - contiguous} later-milestone ops remain in the capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
