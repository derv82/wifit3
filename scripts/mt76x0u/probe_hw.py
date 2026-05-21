"""M0 — read-only HW probe for MT76x0U bring-up.

Reads the USB descriptor only. NO vendor writes, NO FW reset, NO interface
claim beyond what PyUSB does implicitly to read config descriptors. Safe to
run repeatedly without bricking the device.

Goals (per [[feedback_chipset_methodology]] + [[feedback_usb_speed_check]]):
  1. Identify which mt76x0u-family VID:PID the card is enumerating as.
  2. Dump bcdUSB / speed / EP layout so we can verify it matches the
     kernel's positional `mt76u_set_endpoints` assignment BEFORE writing
     any transport code (per [[feedback_batch_audit_constants]]).
  3. Detect mass-storage stub mode (pre-mode-switch) vs wireless mode.

Usage:
    uv run python scripts/mt76x0u/probe_hw.py
    .venv\\Scripts\\python.exe scripts/mt76x0u/probe_hw.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util


# Kernel mt76x0u id_table — data_dumps/mt76-source-v6.18/mt76x0/usb.c:14-43.
# Format: (vid, pid, "vendor/model")
MT76X0U_USB_IDS: list[tuple[int, int, str]] = [
    (0x148F, 0x7610, "MediaTek MT7610U reference"),
    (0x13B1, 0x003E, "Linksys AE6000"),
    (0x0E8D, 0x7610, "Sabrent NTWLAC / MediaTek MT7610U"),
    (0x7392, 0xa711, "Edimax 7711mac"),
    (0x7392, 0xb711, "Edimax / Elecom"),
    (0x148F, 0x761a, "TP-Link TL-WDN5200"),
    (0x148F, 0x760a, "TP-Link (unknown)"),
    (0x0B05, 0x17d1, "Asus USB-AC51"),
    (0x0B05, 0x17db, "Asus USB-AC50"),
    (0x0DF6, 0x0075, "Sitecom WLA-3100"),
    (0x2019, 0xab31, "Planex GW-450D"),
    (0x2001, 0x3d02, "D-Link DWA-171 rev B1"),
    (0x0586, 0x3425, "Zyxel NWD6505"),
    (0x07B8, 0x7610, "AboCom AU7212"),
    (0x04BB, 0x0951, "I-O DATA WN-AC433UK"),
    (0x057C, 0x8502, "AVM FRITZ!WLAN USB Stick AC 430"),
    (0x293C, 0x5702, "Comcast Xfinity KXW02AAA"),
    (0x20F4, 0x806b, "TRENDnet TEW-806UBH"),
    (0x7392, 0xc711, "Devolo Wifi ac Stick"),
    (0x0DF6, 0x0079, "Sitecom Europe ac Stick"),
    (0x2357, 0x0123, "TP-Link T2UHP_US_v1"),
    (0x2357, 0x010b, "TP-Link T2UHP_UN_v1"),
    (0x2357, 0x0105, "TP-Link Archer T1U"),
    (0x0E8D, 0x7630, "MediaTek MT7630U"),
    (0x0E8D, 0x7650, "MediaTek MT7650U"),
]

# For sanity: also flag if the card is enumerating as the *MT7612U* sibling
# (already supported by chips/mt76x2u/). Lets us catch the "AWUS036ACM
# variant ambiguity" — some ACMs are 7612, some 7610.
MT76X2U_SENTINEL_PIDS = {0x7612, 0x7632, 0x7662}

# Mass-storage stub: pre-mode-switch enumeration of mt76x2u (and likely
# mt76x0u — needs hw confirmation). bInterfaceClass=0x08, single bulk
# IN/OUT pair. See mt76x2u MT76X2U.md "Cold-boot mass-storage stub".
USB_CLASS_MASS_STORAGE = 0x08

# Expected wireless-mode endpoint layout per kernel mt76x02_usb.h +
# mt76u_set_endpoints positional assignment:
#   in_ep[0]  = MT_EP_IN_PKT_RX      (some IN)
#   in_ep[1]  = MT_EP_IN_CMD_RESP    (next IN)
#   out_ep[0] = MT_EP_OUT_INBAND_CMD (first OUT)   ← FW upload + MCU
#   out_ep[1..5] = AC_BE / AC_BK / AC_VI / AC_VO / HCCA
# For MT76x2U on this dev box that materialised as:
#   in : 0x84, 0x85   out: 0x08, 0x04, 0x05, 0x06, 0x07, 0x09
# We just dump what we see and let the user compare.


def usb_speed_name(speed: int) -> str:
    """Decode the integer PyUSB speed attribute."""
    return {
        1: "LOW (1.5 Mbps)",
        2: "FULL (12 Mbps)",
        3: "HIGH (480 Mbps)",
        4: "SUPER (5 Gbps)",
        5: "SUPER_PLUS (10 Gbps)",
    }.get(speed, f"UNKNOWN({speed})")


def ep_dir(addr: int) -> str:
    return "IN " if (addr & 0x80) else "OUT"


def ep_type(attrs: int) -> str:
    return {
        0: "CONTROL",
        1: "ISOCHRONOUS",
        2: "BULK",
        3: "INTERRUPT",
    }.get(attrs & 0x03, "?")


def safe_str(dev: usb.core.Device, idx: int) -> str:
    if not idx:
        return "<none>"
    try:
        return usb.util.get_string(dev, idx) or "<empty>"
    except Exception as e:
        return f"<unreadable: {type(e).__name__}>"


def find_mt76x0u_candidate(backend) -> tuple[usb.core.Device, tuple[int, int, str]] | None:
    """Scan the bus for an mt76x0u-table match. Also flag mt76x2u sentinel
    matches so we surface the ACM-variant ambiguity loudly."""
    for vid, pid, desc in MT76X0U_USB_IDS:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is not None:
            return dev, (vid, pid, desc)
    # mt76x2u sentinel check — only if no mt76x0u match.
    for dev in usb.core.find(find_all=True, backend=backend):
        if dev.idVendor == 0x0E8D and dev.idProduct in MT76X2U_SENTINEL_PIDS:
            return dev, (
                dev.idVendor,
                dev.idProduct,
                f"!! MT76x2U SIBLING (PID 0x{dev.idProduct:04x}) — "
                "this card is the mt76x2u family, already supported by chips/mt76x2u/",
            )
    return None


def dump_descriptor(dev: usb.core.Device, matched: tuple[int, int, str]) -> None:
    vid, pid, desc = matched
    print(f"=== mt76x0u candidate: {desc} ({vid:04x}:{pid:04x}) ===\n")

    # ---- Top-level device descriptor.
    print("[device]")
    print(f"  idVendor          : 0x{dev.idVendor:04x}")
    print(f"  idProduct         : 0x{dev.idProduct:04x}")
    print(f"  bcdUSB            : 0x{dev.bcdUSB:04x}  "
          f"({(dev.bcdUSB >> 8):x}.{((dev.bcdUSB >> 4) & 0xF):x}.{(dev.bcdUSB & 0xF):x})")
    print(f"  bcdDevice         : 0x{dev.bcdDevice:04x}")
    try:
        print(f"  speed             : {usb_speed_name(dev.speed)}")
    except Exception as e:
        print(f"  speed             : <unreadable: {e}>")
    print(f"  bDeviceClass      : 0x{dev.bDeviceClass:02x}")
    print(f"  bMaxPacketSize0   : {dev.bMaxPacketSize0}")
    print(f"  bus / address     : {dev.bus} / {dev.address}")
    print(f"  iManufacturer     : {safe_str(dev, dev.iManufacturer)}")
    print(f"  iProduct          : {safe_str(dev, dev.iProduct)}")
    print(f"  iSerialNumber     : {safe_str(dev, dev.iSerialNumber)}")
    print()

    # ---- Walk configurations / interfaces / endpoints.
    in_eps: list[int] = []
    out_eps: list[int] = []
    mass_storage_seen = False

    for cfg in dev:
        print(f"[configuration {cfg.bConfigurationValue}]  "
              f"bNumInterfaces={cfg.bNumInterfaces}  "
              f"bmAttributes=0x{cfg.bmAttributes:02x}  "
              f"MaxPower={cfg.bMaxPower * 2} mA")
        for intf in cfg:
            tag = (
                "  MASS-STORAGE STUB"
                if intf.bInterfaceClass == USB_CLASS_MASS_STORAGE
                else ""
            )
            if intf.bInterfaceClass == USB_CLASS_MASS_STORAGE:
                mass_storage_seen = True
            print(f"  [interface {intf.bInterfaceNumber}.{intf.bAlternateSetting}]  "
                  f"class=0x{intf.bInterfaceClass:02x}  "
                  f"subclass=0x{intf.bInterfaceSubClass:02x}  "
                  f"protocol=0x{intf.bInterfaceProtocol:02x}  "
                  f"numEndpoints={intf.bNumEndpoints}{tag}")
            for ep in intf:
                ep_attr = ep.bmAttributes
                mp = ep.wMaxPacketSize
                slot_label = ""
                if ep_attr & 0x03 == 2:  # BULK
                    if ep.bEndpointAddress & 0x80:
                        slot_label = f"  (would be in_ep[{len(in_eps)}])"
                        in_eps.append(ep.bEndpointAddress)
                    else:
                        slot_label = f"  (would be out_ep[{len(out_eps)}])"
                        out_eps.append(ep.bEndpointAddress)
                print(
                    f"    ep 0x{ep.bEndpointAddress:02x} "
                    f"{ep_dir(ep.bEndpointAddress)} "
                    f"{ep_type(ep_attr)}  "
                    f"maxPacketSize={mp}{slot_label}"
                )
        print()

    # ---- Summary + actionable assessment.
    print("=== summary ===")
    print(f"  bulk IN  endpoints : {[f'0x{a:02x}' for a in in_eps]}")
    print(f"  bulk OUT endpoints : {[f'0x{a:02x}' for a in out_eps]}")
    print(f"  bcdUSB / speed     : 0x{dev.bcdUSB:04x} / {usb_speed_name(dev.speed)}")
    print()

    # ---- Mass-storage stub mode = wrong; user needs Zadig'd to the
    # wireless interface (post-mode-switch). Flag loudly.
    if mass_storage_seen and not in_eps:
        print("[WARN] Device is in MASS-STORAGE STUB mode (pre-mode-switch).")
        print("       Open it once with libusb to trigger the wireless re-enumeration,")
        print("       OR re-Zadig the post-mode-switch wireless interface. See")
        print("       chips/mt76x2u/MT76X2U.md 'Cold-boot mass-storage stub'.")
        return

    # ---- Compare to mt76x2u's known-good layout.
    mt76x2u_in = [0x84, 0x85]
    mt76x2u_out = [0x08, 0x04, 0x05, 0x06, 0x07, 0x09]
    if in_eps == mt76x2u_in and out_eps == mt76x2u_out:
        print("[OK ] EP layout matches mt76x2u (2 in / 6 out, same addresses).")
        print("      mt76u_set_endpoints positional assignment likely holds for mt76x0u too.")
    else:
        print("[INFO] EP layout differs from mt76x2u's known-good layout.")
        print(f"       mt76x2u (ref): in={mt76x2u_in}  out={mt76x2u_out}")
        print("       That's not necessarily wrong — mt76x0u is 1T1R vs 2T2R and may")
        print("       expose a different EP set. We just need to map them to the kernel's")
        print("       positional in_ep[] / out_ep[] slots (see mt76x02_usb.h enum).")

    # ---- USB speed sanity (per [[feedback_usb_speed_check]]).
    if dev.speed == 3:
        print("[INFO] Running at HIGH-SPEED (USB 2.0). Expected for MT7610U.")
    elif dev.speed >= 4:
        print("[INFO] Running at SUPER-SPEED. Verify if mt76x0u actually supports SS")
        print("       (the kernel driver targets USB 2.0; if hub is SS, port may need")
        print("       quirks).")
    else:
        print(f"[WARN] Running at {usb_speed_name(dev.speed)} — below USB 2.0 HIGH.")


def main() -> int:
    print("[*] Scanning USB bus for mt76x0u-family devices "
          f"({len(MT76X0U_USB_IDS)} VID:PID entries from kernel id_table)...")
    backend = libusb_package.get_libusb1_backend()
    hit = find_mt76x0u_candidate(backend)
    if hit is None:
        print("[FAIL] No mt76x0u-family device found on the USB bus.")
        print("       Make sure the card is plugged in and Zadig has bound it to WinUSB.")
        return 1
    dev, matched = hit
    dump_descriptor(dev, matched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
