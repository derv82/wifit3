"""Throwaway: what monitor/RX state does cold_bringup actually leave on the chip?

Wraps ReplayDevice to log every host write to the monitor/RX-path registers as the
REAL driver.connect() (cold path, no loop) runs against the capture. Prints the op
index + pcap frame of each, and where cold_bringup ends — so we can see whether the
working promiscuous-monitor config (RCR 0x90000001 + RXFLTMAP 0xffff, seen on the
wire at frame ~16857) is inside cold_bringup or out in the operational phase the
running connect() never replays.

    uv run python scripts/rtl8821cu_dkms/trace_cold.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"

_WATCH = {0x0608: "RCR", 0x0102: "MSR", 0x06A0: "RXFLTMAP_mgmt",
          0x06A2: "RXFLTMAP_ctrl", 0x06A4: "RXFLTMAP_data", 0x0100: "REG_CR",
          0x0808: "0x808_CCK"}


class LoggingReplay(rp.ReplayDevice):
    def __init__(self, ops):
        super().__init__(ops)
        self.log: list[tuple[int, int, int, int, int]] = []

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength,
                      timeout=None):
        is_write = not (bmRequestType & 0x80)
        op_i, op = self.i, self.ops[self.i] if self.i < len(self.ops) else None
        ret = super().ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                    data_or_wLength, timeout)
        if is_write and wValue in _WATCH and op is not None:
            data = bytes(data_or_wLength)
            self.log.append((op_i, op["frame"], wValue, len(data),
                             int.from_bytes(data, "little")))
        return ret


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value


def main() -> int:
    pcap = DEFAULT_CAP
    dev = rp.find_card_device(pcap)
    ops = rp.merge_ops_by_frame(rp.extract_ctrl_ops(pcap, dev),
                                rp.extract_bulk_out_ops(pcap, dev))
    rdev = LoggingReplay(ops)
    drv = Rtl8821cuDkmsDriver(rdev)
    _run(drv.connect())              # no running loop -> cold path only (driver.py:127)
    end_i = rdev.i
    end_frame = ops[end_i]["frame"] if end_i < len(ops) else ops[-1]["frame"]

    print(f"cold_bringup ran {end_i} ops, ending at pcap frame ~{end_frame}\n")
    print("monitor/RX-path writes emitted by cold_bringup (op# : frame : reg = val):")
    for op_i, frame, reg, width, val in rdev.log:
        print(f"  op{op_i:>6} f{frame:>7} : {_WATCH[reg]:<14} "
              f"0x{reg:04x}/{width} = 0x{val:0{width*2}x}")

    # The state cold_bringup leaves: last write to each watched register.
    last: dict[int, tuple[int, int, int]] = {}
    for op_i, frame, reg, width, val in rdev.log:
        last[reg] = (frame, width, val)
    print("\nFINAL monitor/RX state left by cold_bringup:")
    for reg, name in _WATCH.items():
        if reg in last:
            frame, width, val = last[reg]
            print(f"  {name:<14} 0x{reg:04x} = 0x{val:0{width*2}x}  (last set @ frame {frame})")
        else:
            print(f"  {name:<14} 0x{reg:04x} = (never written by cold_bringup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
