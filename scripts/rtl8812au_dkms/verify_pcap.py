"""Self-contained byte-for-byte replay-diff of the rtl8812au_dkms port against a vendor
cold-boot capture.

PASS means ONLY: for this captured boot, the port emits the same USB bytes the vendor
driver did. It does NOT mean the code is correct or robust (the driver may poll a loop
that iterated once on this boot where we hardcode one write -- same bytes today, divergent
behaviour on a colder boot). The only real pass is beacons off the antenna. This is a
faithfulness gate, not a correctness proof.

Owns its verification end to end -- no shared engine, no driver core. Just tshark
extraction + a replay transport that feeds back the chip's recorded reads (so
read-modify-writes see the real values) and checks every write + FW-page byte-for-byte,
raising at the first mismatch. Fully offline -- no hardware.

Capture-agnostic: auto-detects the card's USB device address and replays its entire vendor
transaction stream from the first op (the bring-up consumes the prefix it needs; the rest
is ignored). No per-capture frame or device constants. Run it against more than one capture
-- a stream that matches one boot but diverges on another is dynamic/state-dependent
behaviour we flattened.

    uv run python scripts/rtl8812au_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8812au_dkms import (  # noqa: E402
    bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower,
)

VENDOR_REQ = "5"            # Realtek USB vendor register access: bRequest 0x05
REG_SYS_CFG = 0x00F0
CHANNEL = 1
DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8812au" / "capture-2.pcap"


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
    """Device address issuing the most vendor (bRequest 0x05) transfers == the card.
    Auto-detected so the harness never pins to a per-capture device number."""
    rows = _tshark(pcap, f"usb.setup.bRequest=={VENDOR_REQ}", ["usb.device_address"])
    counts = Counter(r[0] for r in rows if r[0])
    if not counts:
        raise SystemExit(f"no vendor (bRequest 0x05) traffic in {pcap.name}")
    return int(counts.most_common(1)[0][0])


def extract_ops(pcap: Path, dev: int) -> list[dict]:
    """Ordered vendor ops for the card across the WHOLE capture.

    Submit ('S')/completion ('C') are adjacent rows (urb_id is reused). Write data is on
    the submit (usb.data_fragment); read data on the completion (usb.control.Response);
    bulk-OUT data on the submit (usb.capdata). bmRequestType 0x40 = write, 0xC0 = read.
    """
    fields = ["frame.number", "usb.urb_type", "usb.transfer_type", "usb.setup.bRequest",
              "usb.bmRequestType", "usb.setup.wValue", "usb.setup.wLength",
              "usb.data_fragment", "usb.capdata", "usb.control.Response"]
    flt = (f"usb.device_address=={dev} && "
           "(usb.transfer_type==0x02 || "
           "(usb.transfer_type==0x03 && usb.endpoint_address.direction==0))")
    rows = []
    for c in _tshark(pcap, flt, fields):
        frame, utype, ttype, breq, brt, wval, wlen, dfrag, cap, resp = c[:10]
        rows.append({"frame": int(frame), "utype": utype.strip("'"), "ttype": int(ttype, 0),
                     "breq": breq, "brt": brt, "wval": wval, "wlen": wlen,
                     "dfrag": _hex(dfrag), "cap": _hex(cap), "resp": _hex(resp)})
    ops, pending = [], None
    for r in rows:
        if r["utype"] == "S":
            if r["ttype"] == 3:                                   # bulk OUT
                ops.append({"kind": "B", "data": r["cap"] or r["dfrag"], "frame": r["frame"]})
            elif r["ttype"] == 2 and r["breq"] == VENDOR_REQ:
                addr, width, brt = int(r["wval"], 0), int(r["wlen"], 0), int(r["brt"], 0)
                if brt == 0x40:                                  # write -- value on submit
                    ops.append({"kind": "W", "addr": addr, "width": width,
                                "value": int.from_bytes(r["dfrag"], "little"), "frame": r["frame"]})
                elif brt == 0xC0:                                # read -- value on completion
                    pending = {"addr": addr, "width": width, "frame": r["frame"]}
        elif r["utype"] == "C" and r["ttype"] == 2 and pending is not None:
            ops.append({"kind": "R", "addr": pending["addr"], "width": pending["width"],
                        "value": int.from_bytes(r["resp"], "little"), "frame": pending["frame"]})
            pending = None
    return ops


_TT = {"0x00": "iso", "0x01": "interrupt", "0x02": "control", "0x03": "bulk"}


def audit_coverage(pcap: Path, dev: int) -> bool:
    """Confront the COMPLETE card traffic so the replay can never be silently blind to an
    endpoint or transfer-type.

    Driver-CONSTRUCTED bytes (vendor control + bulk-OUT) are what we reproduce and verify;
    chip->host data (bulk-IN RX, interrupt-IN events) is input we consume/ignore, not output
    we reproduce. If the card uses any *other* channel -- an interrupt the driver reads and
    acts on, an unexpected bulk-OUT -- the replay would be blind to it and a 100% PASS would
    be a lie. This prints the full breakdown and returns False on any such blind spot.
    """
    rows = _tshark(pcap, f"usb.device_address=={dev}",
                   ["usb.transfer_type", "usb.endpoint_address"])
    seen: Counter = Counter()
    for r in rows:
        ttype = r[0] if r else ""
        ep = r[1] if len(r) > 1 else ""
        if ttype:
            seen[(ttype, ep)] += 1
    print(f"  coverage audit (dev{dev}), packets incl. completions:")
    ok = True
    for (ttype, ep), n in sorted(seen.items()):
        name = _TT.get(ttype, f"type{ttype}")
        is_in = bool(ep) and (int(ep, 16) & 0x80)
        if ttype == "0x02":
            tag = "REPRODUCE  (control: vendor regs/FW; std enumeration is OS-level)"
        elif ttype == "0x03" and not is_in:
            tag = "REPRODUCE  (bulk-OUT: FW/TX)"
        elif ttype == "0x03" and is_in:
            tag = "input only (bulk-IN RX, chip->host -- the thing we're fixing)"
        else:
            tag = f"** BLIND SPOT: {name} {'IN' if is_in else 'OUT'} -- driver may use this **"
            ok = False
        print(f"     {name:9} ep {ep or '--':>4}  {n:>7}  {tag}")
    if not ok:
        print("  !! the card uses a channel the replay does NOT reproduce -- investigate "
              "before trusting any PASS")
    return ok


class Divergence(AssertionError):
    pass


class ReplayTransport:
    """Drop-in for the chip transport surface (read/write/writeN/bulk_out). Reads return
    recorded chip values; writes/bulk are checked byte-for-byte. First mismatch raises."""

    def __init__(self, ops):
        self.ops, self.i = ops, 0

    def _next(self, want):
        if self.i >= len(self.ops):
            raise Divergence(f"port emitted extra {want} past end of capture")
        op = self.ops[self.i]
        self.i += 1
        return op

    @staticmethod
    def _fmt(op):
        if op["kind"] == "B":
            return f"bulk[{len(op['data'])}B] @f{op['frame']}"
        return (f"{op['kind']} 0x{op['addr']:04x}/{op['width']}"
                f"=0x{op['value']:0{op['width'] * 2}x} @f{op['frame']}")

    def _read(self, addr, width):
        op = self._next(f"read(0x{addr:04x}/{width})")
        if op["kind"] != "R" or op["addr"] != addr or op["width"] != width:
            raise Divergence(f"port read 0x{addr:04x}/{width}, capture has {self._fmt(op)}")
        return op["value"]

    def _write(self, addr, width, value):
        op = self._next(f"write(0x{addr:04x}/{width})")
        if (op["kind"] != "W" or op["addr"] != addr or op["width"] != width
                or op["value"] != value):
            raise Divergence(f"port wrote 0x{addr:04x}/{width}=0x{value:0{width * 2}x}, "
                             f"capture has {self._fmt(op)}")

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
        data = bytes(data)
        op = self._next(f"writeN(0x{addr:04x}/{len(data)})")
        if op["kind"] != "W" or op["addr"] != addr or op["width"] != len(data):
            raise Divergence(f"port wrote 0x{addr:04x}/{len(data)}B, capture has {self._fmt(op)}")
        cap = op["value"].to_bytes(op["width"], "little")
        if data != cap:
            j = next((k for k in range(len(data)) if data[k] != cap[k]), len(data))
            raise Divergence(f"writeN 0x{addr:04x} differs at byte {j} @f{op['frame']}")

    def bulk_out(self, data):
        op = self._next(f"bulk[{len(data)}B]")
        if op["kind"] != "B":
            raise Divergence(f"port sent bulk[{len(data)}B], capture has {self._fmt(op)}")
        if bytes(data) != op["data"]:
            n = min(len(data), len(op["data"]))
            j = next((k for k in range(n) if data[k] != op["data"][k]), n)
            raise Divergence(f"bulk differs at byte {j} (port {len(data)}B/cap "
                             f"{len(op['data'])}B) @f{op['frame']}")


class _SmokeTransport:
    """No-op transport: lets a function (e.g. monitor.enter_monitor) run for Python errors
    without diffing against a capture. Reads return 0; writes are dropped."""

    def read8(self, a):
        return 0

    read16 = read32 = read8

    def write8(self, a, v):
        pass

    write16 = write32 = writeN = write8


def main() -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = find_card_device(pcap)
    audit_coverage(pcap, dev)
    ops = extract_ops(pcap, dev)
    n_w = sum(o["kind"] == "W" for o in ops)
    n_r = sum(o["kind"] == "R" for o in ops)
    n_b = sum(o["kind"] == "B" for o in ops)
    print(f"{pcap.name}: card=dev{dev}, {len(ops)} vendor ops ({n_r} R, {n_w} W, {n_b} bulk)")
    if not ops:
        return 1
    print(f"  first op: {ReplayTransport._fmt(ops[0])}")

    # build_jaguar_params needs REG_SYS_CFG; PEEK the recorded read (read_chip_params
    # already issues the driver's single 0xF0 read -- a second one would be a false diff).
    sys_cfg = next((o["value"] for o in ops if o["kind"] == "R" and o["addr"] == REG_SYS_CFG),
                   None)
    if sys_cfg is None:
        print("FAIL: no recorded 0xF0 (REG_SYS_CFG) read to seed build_jaguar_params")
        return 1

    t = ReplayTransport(ops)
    fw = firmware.load_firmware_blob()
    miles = []
    try:
        p = efuse.read_chip_params(t)
        miles.append(("efuse", t.i))
        jp = efuse.build_jaguar_params(p, sys_cfg)
        firmware.bring_up(t, fw)
        miles.append(("M1 fw", t.i))
        mac.phy_mac_config(t)
        mac.mac_init_misc(t)
        miles.append(("M2 mac", t.i))
        bb.phy_bb_config(t, crystal_cap=p.crystal_cap, params=jp)
        rf.phy_rf_config(t, params=jp)
        miles.append(("M3 bb+rf", t.i))
        chan.set_chnl_bw(t, ch=CHANNEL, bb_swing_2g_a=p.bb_swing_2g[0],
                         bb_swing_2g_b=p.bb_swing_2g[1], rfe_type=p.rfe_type)
        miles.append(("M4 chan", t.i))
        txpower.set_tx_power(t, CHANNEL, p.tx_power_2g)
        miles.append(("M-TXPWR", t.i))
        mac.hal_init_misc_pre(t)
        dig.init_hal_dm(t, search_edcca=True)   # the live PWDB-EDCCA search reproduces against
        mac.hal_init_misc_post(t)               # the capture's own recorded PSD reads -- not stripped
        miles.append(("M5 init-dm", t.i))
        # The deterministic RX-configuring bring-up ends here. What follows in the capture is
        # airmon's monitor opmode set (RCR=0x90000001 + MAC-addr + a redundant channel/TX-power
        # re-tune) -- a STA-driver path wifit3 does not walk: it enters monitor directly with
        # its own accept-all RCR (0x9000382F). monitor.enter_monitor is therefore NOT byte-
        # diffable against this capture; it is exercised here for errors and verified live by
        # the beacon count.
        monitor.enter_monitor(_SmokeTransport())
    except Divergence as e:
        print(f"\nFAIL @ first divergence:\n  {e}")
        print(f"  reproduced {t.i} of {len(ops)} ops; "
              f"last clean milestone: {miles[-1][0] if miles else '(none)'}")
        return 1
    except Exception as e:  # noqa: BLE001 - surface harness/port errors distinctly
        print(f"\nERROR (harness/port bug, not a divergence): {type(e).__name__}: {e} @ op {t.i}")
        return 2

    print(f"\nPASS: reproduced {t.i}/{len(ops)} ops byte-for-byte through the deterministic "
          f"RX bring-up (M5 init-dm). The monitor opmode entry is wifit3's always-monitor "
          f"deviation -- verified live by the beacon count, not this byte diff.")
    prev = 0
    for label, end in miles:
        print(f"      {label:12} {end - prev:5} ops")
        prev = end
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
