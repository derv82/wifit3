"""Replay-diff the rtl8192cu probe and rtl92cu_hw_init against the RTL8188CUS cold-boot capture.

The capture opens with USB-core enumeration (GET_DESCRIPTOR / SET_CONFIGURATION); those frames
carry no Realtek vendor request and are not driver ops. The window starts at the first vendor
op (rtl92c_read_chip_version's REG_SYS_CFG read) and ends before the first REG_RCR read, the
first op after rtl92cu_hw_init returns (rtl_init_rx_config only copies mac->rx_conf, base.c:590).
The endpoint descriptors fed to _rtl_usb_init come from the capture's own configuration descriptor.

Run: uv run python scripts/chips/rtl8192cu/verify_pcap.py [capture-2]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.hw import rtl92cu_hw_init
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.chips.rtl8192cu.usb import UsbEndpointDescriptor, rtl_usb_probe

CAP_DIR = REPO / "driver_captures" / "captures_rtl8192cu_caponly"


class ReplayIo(rp.ReplayTransport):
    """rtlpriv->io over the replay: rtl_write_chunk is the wide control write."""

    def write_chunk(self, addr, data):
        self.writeN(addr, data)


def _endpoints(pcap: Path, dev: int) -> tuple[list[UsbEndpointDescriptor], bool]:
    """The card's endpoint descriptors from its recorded configuration descriptor."""
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y", f"usb.device_address=={dev} && usb.bEndpointAddress",
         "-T", "fields", "-e", "usb.bEndpointAddress", "-e", "usb.bmAttributes", "-e", "usb.wMaxPacketSize"],
        capture_output=True, text=True, check=True).stdout
    line = next(row for row in out.splitlines() if row.strip())
    addrs, attrs, sizes = (col.split(",") for col in line.split("\t")[:3])
    endpoints = [UsbEndpointDescriptor(int(a, 0), int(b, 0)) for a, b in zip(addrs, attrs)]
    is_high_speed = any(int(s, 0) == C.USB_HIGH_SPEED_BULK_SIZE for s in sizes)
    return endpoints, is_high_speed


def _first_op(ops, kind: str, addr: int) -> int:
    return next(i for i, o in enumerate(ops) if o["kind"] == kind and o["addr"] == addr)


def _stopped(rt: ReplayIo, ops, what: str, at: int) -> int:
    print(f"  FAIL: {what} stopped at {rt.i}/{at}; next capture op {rt._fmt(ops[rt.i])} "
          f"(frame {ops[rt.i]['frame']})")
    return 1


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None
    name = Path(cap or "capture-2").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    endpoints, is_high_speed = _endpoints(pcap, dev)
    hw_init_start = _first_op(ops, "R", C.REG_APS_FSMCO)
    end = _first_op(ops, "R", C.REG_RCR)
    print(f"{name}: card=dev{dev}, {len(ops)} driver ops; USB-core enumeration precedes frame "
          f"{ops[0]['frame']}")
    print(f"  window: probe ops 0..{hw_init_start - 1}, rtl92cu_hw_init ops {hw_init_start}..{end - 1}; "
          f"endpoints {[hex(e.bEndpointAddress) for e in endpoints]}, high-speed={is_high_speed}")

    rt = ReplayIo(ops[:end])
    rtlpriv = RtlPriv(rt)
    try:
        rtl_usb_probe(rtlpriv, endpoints, is_high_speed)
        if rt.i != hw_init_start:
            return _stopped(rt, ops, "probe", hw_init_start)
        err = rtl92cu_hw_init(rtlpriv)
    except rp.Divergence as e:
        print(f"  FAIL at {rt.i}/{end}:\n    {e}")
        return 1
    if err:
        print(f"  FAIL: rtl92cu_hw_init returned {err} at {rt.i}/{end}")
        return 1
    if rt.i != end:
        return _stopped(rt, ops, "hw_init", end)

    hal, efuse, usb, phy = rtlpriv.rtlhal, rtlpriv.efuse, rtlpriv.usb, rtlpriv.phy
    print(f"  PASS: {rt.i}/{end} ops byte-for-byte")
    print(f"      version=0x{hal.version:02x} rf_type={phy.rf_type} board_type={efuse.board_type} "
          f"autoload_fail={efuse.autoload_failflag} efuse_used={efuse.efuse_usedbytes}B "
          f"eeprom_did=0x{efuse.eeprom_did:04x} oem_id={hal.oem_id} fw={hal.fw_name}")
    print(f"      out_eps={usb.out_eps[:usb.out_ep_nums]} out_queue_sel=0x{usb.out_queue_sel:x} "
          f"ep_mapping={usb.ep_mapping}")
    print(f"      fw v{hal.fw_version}.{hal.fw_subversion} fw_ready={hal.fw_ready} "
          f"rfreg_chnlval=[0x{phy.rfreg_chnlval[0]:05x}, 0x{phy.rfreg_chnlval[1]:05x}] "
          f"iqk e94/e9c=0x{phy.reg_e94:x}/0x{phy.reg_e9c:x}")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
