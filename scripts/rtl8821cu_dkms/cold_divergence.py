"""Find where our REAL cold bring-up diverges from the Linux capture on silicon.

The byte-gate (verify_pcap) feeds the driver the CAPTURED read values, so any read-modify-write
that is subtly wrong only diverges when the REAL chip returns a different value — it passes the gate
and is wrong on hardware. This drives the REAL driver against real silicon while walking the capture
op stream in lockstep: for every vendor ctrl transfer it compares the real read value (or the value
we WROTE, computed from real reads) against the capture, with tolerant resync past the variable-
length poll loops (FW-ready, mac-hidden, stop_ic_trx idle). The first non-volatile divergence is the
locus where our chip state departs from Linux's.

After bring-up it drains bulk-IN across ch1+ch36 to label the launch GOOD/DEAD, so divergences can
be correlated with RX death across several runs (run it a few times). Passive (RX only).

    uv run python scripts/rtl8821cu_dkms/cold_divergence.py [iters] [dwell_s]
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import libusb_package
import usb.core
import usb.util

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8821cu_dkms import bringup, chan
from wifit3.chips.rtl8821cu_dkms.rf import read_rf, write_rf
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport

CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"
USB_VID, USB_PID, FW_EP, WIFI_CLASS = 0x0BDA, 0xC820, 0x05, 0xFF
_SIG = re.compile("8000[0-9a-f]{4}ffffffffffff[0-9a-f]{24}")
_RESYNC_W = 1500

# Reads whose value legitimately varies run-to-run (status / counters / mailbox / measurement /
# FIFO ptr / RF-readback-of-live-state) — a mismatch here is not a port bug. Everything else is
# a config/cap/table register: a mismatch means our chip state has departed from the capture.
_VOLATILE = {
    0x01A0, 0x01A1, 0x01A2, 0x01A3, 0x01A4, 0x01A5, 0x01A6, 0x01A7,  # C2H mailbox
    0x00AA, 0x00AB,                                                  # BT scoreboard
    0x0288, 0x0210, 0x1118, 0x0050, 0x0051,                          # DMA status / RXFF ptr
    0x0C50, 0x0FA0, 0x0F48, 0x0A5C, 0x09A4,                          # IGI / dbgport / FA cnt
    0x0080, 0x0081,                                                  # power-state poll
}


class TraceDev:
    """ctrl_transfer-level shim: real I/O + lockstep compare to the capture op stream."""

    def __init__(self, real, ops):
        self.real, self.ops, self.i = real, ops, 0
        self.div: list[dict] = []
        self.recording = True
        self.aligned = self.our_extra = 0

    def _match(self, want_dir, wval, widx, width):
        for j in range(self.i, min(self.i + _RESYNC_W, len(self.ops))):
            o = self.ops[j]
            if (o.get("dir") == want_dir and o.get("wval") == wval
                    and o.get("widx") == widx and o.get("width") == width):
                self.i = j + 1
                self.aligned += 1
                return o
        self.our_extra += 1
        return None

    def ctrl_transfer(self, brt, breq, wValue, wIndex, data_or_len, timeout=None):
        is_in = bool(brt & 0x80)
        ret = self.real.ctrl_transfer(brt, breq, wValue, wIndex, data_or_len, timeout)
        if not self.recording:
            return ret
        if is_in:
            real = bytes(ret)
            op = self._match("IN", wValue, wIndex, len(real))
            if op is not None and op["data"] != real and wValue not in _VOLATILE:
                self.div.append({"op": self.i - 1, "frame": op["frame"], "dir": "R",
                                 "addr": wValue, "width": len(real),
                                 "real": int.from_bytes(real, "little"),
                                 "cap": int.from_bytes(op["data"], "little")})
        else:
            data = bytes(data_or_len) if data_or_len else b""
            op = self._match("OUT", wValue, wIndex, len(data))
            if op is not None and op["data"] != data and wValue != 0x04E0:
                self.div.append({"op": self.i - 1, "frame": op["frame"], "dir": "W",
                                 "addr": wValue, "width": len(data),
                                 "real": int.from_bytes(data, "little"),
                                 "cap": int.from_bytes(op["data"], "little")})
        return ret

    def write(self, endpoint, data, timeout=None):
        ret = self.real.write(endpoint, data, timeout)
        if self.recording:
            # bulk-OUT: align by scanning forward for the next BULK op (FW pages are deterministic)
            for j in range(self.i, min(self.i + _RESYNC_W, len(self.ops))):
                if self.ops[j].get("dir") == "BULK":
                    self.i = j + 1
                    break
        return ret

    def read(self, endpoint, size, timeout=None):
        return self.real.read(endpoint, size, timeout)

    def get_active_configuration(self):
        return self.real.get_active_configuration()


def _open(backend):
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        return None, None
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    intf = next((i.bInterfaceNumber for i in dev.get_active_configuration()
                 if i.bInterfaceClass == WIFI_CLASS), None)
    try:
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
    except (NotImplementedError, usb.core.USBError):
        pass
    usb.util.claim_interface(dev, intf)
    return dev, intf


def _count_rx(t, dwell: float) -> int:
    n, end = 0, time.monotonic() + dwell
    while time.monotonic() < end:
        buf = t.bulk_in()
        if buf:
            n += len(_SIG.findall(buf.hex()))
    return n


def one(real, ops, dwell: float) -> tuple[str, int, int, list[dict]]:
    td = TraceDev(real, ops)
    t = Rtl8821cuTransport(td, bulk_out_ep=FW_EP)
    info = bringup.cold_bringup(t)
    td.recording = False                      # stop comparing; just drive RX from here
    for _ in range(5):                        # _relatch_2g_band
        rf18 = read_rf(t, 0x18)
        if not (rf18 & (1 << 16)):
            break
        write_rf(t, 0x18, rf18 & ~(1 << 16))
    n1 = _count_rx(t, dwell)
    chan.set_channel(t, info, 36)
    n36 = _count_rx(t, dwell)
    verdict = "GOOD" if (n1 + n36) >= 10 else "DEAD"
    return verdict, n1, n36, td.div


def main() -> int:
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    dwell = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    backend = libusb_package.get_libusb1_backend()
    dev = rp.find_card_device(CAP)
    ops = rp.merge_ops_by_frame(rp.extract_ctrl_ops(CAP, dev),
                                rp.extract_bulk_out_ops(CAP, dev))
    print(f"[*] {len(ops)} capture ops; {iters} launches\n")

    launches = []
    for k in range(iters):
        real, intf = _open(backend)
        if real is None:
            print("no 0bda:c820 device")
            return 1
        try:
            verdict, n1, n36, div = one(real, ops, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"launch {k}: EXCEPTION {type(e).__name__}: {e}")
            div = None
        finally:
            try:
                usb.util.dispose_resources(real)
            except usb.core.USBError:
                pass
        if div is None:
            time.sleep(1.5)
            continue
        # key each divergence by (op_index, dir, addr) -> real value, for cross-launch diff
        keyed = {(d["op"], d["dir"], d["addr"], d["width"]): d["real"] for d in div}
        launches.append({"verdict": verdict, "n1": n1, "n36": n36, "keyed": keyed})
        print(f"launch {k}: {verdict}  ch1={n1:5d} ch36={n36:5d}  ({len(div)} divergences vs capture)")
        time.sleep(1.5)

    good = [L for L in launches if L["verdict"] == "GOOD"]
    dead = [L for L in launches if L["verdict"] == "DEAD"]
    print(f"\n=== {len(good)} GOOD / {len(dead)} DEAD ===")
    if not (good and dead):
        print("need both a GOOD and a DEAD launch to diff — rerun")
        return 0
    # divergence keys whose REAL value is consistent within good and within dead, but DIFFERS
    # between the two groups (or present in one group only) — the per-boot coin-toss signature.
    allkeys = set().union(*[set(L["keyed"]) for L in launches])
    print("keys that separate GOOD from DEAD (real value differs good-vs-dead):")
    found = False
    for key in sorted(allkeys):
        gv = {L["keyed"].get(key) for L in good}
        dv = {L["keyed"].get(key) for L in dead}
        if gv != dv:
            op, d, addr, w = key
            gs = sorted(f"0x{v:0{w*2}x}" if v is not None else "—" for v in gv)
            ds = sorted(f"0x{v:0{w*2}x}" if v is not None else "—" for v in dv)
            print(f"  op{op:>5} {d} 0x{addr:04x}/{w}  good={gs}  dead={ds}")
            found = True
    if not found:
        print("  NONE — the vs-capture divergence set is identical GOOD vs DEAD.")
        print("  => the coin toss is NOT in the digital read/write trace (analog or timing).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
