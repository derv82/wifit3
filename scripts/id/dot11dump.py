"""dot11dump: tap the raw per-interface RX stream and print one line per 802.11 frame.

A standalone probe for device fingerprinting. It brings up one wifit3 USB interface, tunes to a
channel, and registers a raw RX callback (the same pre-ingest tap wep_lab uses). Each frame prints
as `src->dst [name] <fields>`, where fields are a labeled IE tag=value walk plus the IEORDER,
VENDOR, and WSC fingerprint tokens. A frame with nothing past the name is skipped, so encrypted
data frames mostly drop out. `--strings` adds a STR{} pass of printable byte runs (mostly
ciphertext, off by default). Management, data, and EAPOL frames reach the tap; control and corrupt
frames are dropped by the parser before it.

Frame lines go to stdout; a live counter and status go to stderr, so a plain redirect keeps the
capture clean:

    uv run python scripts/id/dot11dump.py --channel 6 > cap.txt
    uv run python scripts/id/dot11dump.py --channel 6 --mac aa:bb:cc:dd:ee:ff
    uv run python scripts/id/dot11dump.py --channel 6 --strings --pcap cap.pcap --seconds 20

This script is passive (RX only): it transmits nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from dev import select_device
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.persist.pcap import write_pcap
from wifit3.dot11.wsc.messages import (
    ATTR_DEV_NAME, ATTR_MANUFACTURER, ATTR_MODEL_NAME, ATTR_MODEL_NUMBER,
    ATTR_PRIMARY_DEV_TYPE, ATTR_SERIAL_NUMBER, parse_tlvs,
)
from wifit3.wlan.fingerprint_vendors import VENDOR_BY_OUI

# 802.11 type/subtype -> short name, derived from the FC byte so every frame gets a name (including
# subtypes the parser only labels "mgmt_N"). Control names are here for completeness though the
# parser drops control frames before the tap.
_NAMES = {
    (0, 0): "assoc_req", (0, 1): "assoc_resp", (0, 2): "reassoc_req", (0, 3): "reassoc_resp",
    (0, 4): "probe_req", (0, 5): "probe_resp", (0, 6): "timing_adv", (0, 8): "beacon",
    (0, 9): "atim", (0, 10): "disassoc", (0, 11): "auth", (0, 12): "deauth",
    (0, 13): "action", (0, 14): "action_noack",
    (1, 8): "bar", (1, 9): "block_ack", (1, 10): "ps_poll", (1, 11): "rts",
    (1, 12): "cts", (1, 13): "ack", (1, 14): "cf_end", (1, 15): "cf_end_ack",
    (2, 0): "data", (2, 4): "null", (2, 8): "qos_data", (2, 12): "qos_null",
}

# Power-save / keepalive frames with no payload: pure noise, dropped unless --all.
_NOISE = {"null", "qos_null"}

# 802.11 element id -> short label, so the TLV map reads as names not numbers. Unknown ids show
# as "IE<n>". IEORDER keeps the raw numbers (easier to eyeball a sequence).
_IE_LABELS = {
    0: "SSID", 1: "SuppRates", 2: "FHParam", 3: "DSParam", 4: "CFParam", 5: "TIM",
    6: "IBSSParam", 7: "Country", 10: "Request", 11: "BSSLoad", 16: "Challenge",
    32: "PowerConstraint", 33: "PowerCap", 35: "TPCReport", 36: "SuppChannels",
    37: "ChannelSwitch", 42: "ERP", 45: "HTCaps", 46: "QoSCaps", 47: "ERPInfo", 48: "RSN",
    50: "ExtSuppRates", 51: "APChannelReport", 52: "NeighborReport", 54: "MobilityDomain",
    59: "SuppOpClasses", 61: "HTOp", 70: "RMEnabledCaps", 71: "MultiBSSID", 72: "BSSCoex",
    74: "OverlapBSSScan", 107: "Interworking", 108: "AdvProtocol", 127: "ExtCaps",
    191: "VHTCaps", 192: "VHTOp", 195: "VHTTxPwr", 201: "RNR", 221: "Vendor", 255: "Ext",
}

# RSN cipher/AKM suite types (for the standard OUI 00-0f-ac), and known vendor OUI+type labels.
_RSN_CIPHER = {0: "UseGroup", 1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 6: "BIP-CMAC128",
               8: "GCMP128", 9: "GCMP256", 10: "CCMP256", 11: "BIP-GMAC128", 12: "BIP-GMAC256",
               13: "BIP-CMAC256"}
_RSN_AKM = {1: "802.1X", 2: "PSK", 3: "FT-802.1X", 4: "FT-PSK", 5: "802.1X-SHA256",
            6: "PSK-SHA256", 7: "TDLS", 8: "SAE", 9: "FT-SAE", 11: "802.1X-SuiteB",
            12: "802.1X-SuiteB192", 13: "FT-802.1X-SHA384", 18: "OWE"}
_VENDOR_TYPE = {b"\x00\x50\xf2\x01": "WPA", b"\x00\x50\xf2\x02": "WMM", b"\x00\x50\xf2\x04": "WPS",
                b"\x50\x6f\x9a\x09": "P2P", b"\x50\x6f\x9a\x10": "HS20", b"\x50\x6f\x9a\x16": "MBO"}
_EXT_LABEL = {35: "HECaps", 36: "HEOp"}   # Element ID Extension sub-ids we name; rest show as Ext<n>

# IE section start = 24-byte MAC header + the subtype's fixed fields. Only these mgmt subtypes carry
# a TLV list; anything else gets a strings-only dump.
_IE_START = {
    0: 28,   # assoc_req:    + capability(2) + listen(2)
    1: 30,   # assoc_resp:   + capability(2) + status(2) + aid(2)
    2: 34,   # reassoc_req:  + capability(2) + listen(2) + current_ap(6)
    3: 30,   # reassoc_resp: as assoc_resp
    4: 24,   # probe_req:    no fixed fields
    5: 36,   # probe_resp:   + timestamp(8) + interval(2) + capability(2)
    8: 36,   # beacon:       as probe_resp
}


def frame_name(raw: bytes) -> str:
    if not raw:
        return "empty"
    fc0 = raw[0]
    return _NAMES.get(((fc0 >> 2) & 0x03, (fc0 >> 4) & 0x0F), f"type{(fc0 >> 2) & 3}_sub{(fc0 >> 4) & 0xF}")


def _val(val: bytes) -> str:
    """A tag value as a quoted string when fully printable, else hex."""
    if val and all(0x20 <= b <= 0x7E for b in val):
        return f"'{val.decode('ascii')}'"
    return val.hex()


def parse_ies(raw: bytes, subtype: int) -> list[tuple[int, bytes]]:
    """[(tag_id, value), ...] for the IE section of a mgmt frame, order preserved. Empty for
    subtypes without an IE list, or once the walk desyncs on a truncated tag."""
    start = _IE_START.get(subtype)
    if start is None or len(raw) <= start:
        return []
    body = raw[start:]
    out, i = [], 0
    while i + 2 <= len(body):
        ln = body[i + 1]
        val = body[i + 2:i + 2 + ln]
        if len(val) != ln:
            break
        out.append((body[i], val))
        i += 2 + ln
    return out


# WSC identity attributes worth surfacing, by WSC attribute id (see wifit3.dot11.wsc.messages).
_WSC_NAMES = {
    ATTR_MANUFACTURER: "mfr", ATTR_MODEL_NAME: "model", ATTR_MODEL_NUMBER: "model_no",
    ATTR_DEV_NAME: "name", ATTR_SERIAL_NUMBER: "serial", ATTR_PRIMARY_DEV_TYPE: "dev_type",
}
_WPS_IE = b"\x00\x50\xf2\x04"   # the Vendor Specific IE (221) OUI+type that carries WSC attributes


def field_ieorder(ies) -> str | None:
    """The sequence of IE tag ids: the IE-order fingerprint (order is unspecified past ids 0, 1)."""
    return "IEORDER{" + ",".join(str(t) for t, _ in ies) + "}" if ies else None


def field_vendor(ies) -> str | None:
    """Every Vendor Specific IE (221) OUI, with its vendor name: identifies the chipset / stack."""
    ouis = []
    for tag, val in ies:
        if tag == 221 and len(val) >= 3:
            name = VENDOR_BY_OUI.get(val[:3].hex().upper())
            entry = ":".join(f"{b:02x}" for b in val[:3]) + (f"({name})" if name else "")
            if entry not in ouis:
                ouis.append(entry)
    return "VENDOR{" + ",".join(ouis) + "}" if ouis else None


def field_wsc(ies) -> str | None:
    """WSC device attributes (make/model/name) from a WPS IE, when the frame carries one."""
    for tag, val in ies:
        if tag == 221 and val[:4] == _WPS_IE and len(val) > 4:
            try:
                attrs = parse_tlvs(val[4:])
            except Exception:  # noqa: BLE001
                return None
            got = [f"{name}={_val(attrs[aid])}" for aid, name in _WSC_NAMES.items() if aid in attrs]
            if got:
                return "WSC{" + ",".join(got) + "}"
    return None


def _squeeze(val: bytes) -> str:
    """Hex, collapsing a trailing zero run to +Nz (N = zero nibbles) only when that is shorter, i.e.
    once the run passes 3 nibbles. Shorter runs print in full."""
    trimmed = val.rstrip(b"\x00")
    znib = (len(val) - len(trimmed)) * 2
    return trimmed.hex() + f"+{znib}z" if znib > 3 else val.hex()


def _rates(val: bytes) -> str:
    """Supported-rate bytes to Mbps, 'b' marking a basic (mandatory) rate."""
    return ",".join(f"{(b & 0x7f) / 2:g}" + ("b" if b & 0x80 else "") for b in val)


def _suite(b: bytes, names: dict) -> str:
    """A 4-byte RSN cipher/AKM suite (OUI + type) to its label, or hex for a non-standard OUI."""
    if len(b) == 4 and b[:3] == b"\x00\x0f\xac":
        return names.get(b[3], f"?{b[3]}")
    return b.hex()


def _rsn(val: bytes) -> str:
    """RSN element: version, group / pairwise ciphers, AKMs, and capability bits, all labeled."""
    try:
        group = _suite(val[2:6], _RSN_CIPHER)
        i = 6
        n = int.from_bytes(val[i:i + 2], "little")
        i += 2
        pair = [_suite(val[i + 4 * k:i + 4 * k + 4], _RSN_CIPHER) for k in range(n)]
        i += 4 * n
        m = int.from_bytes(val[i:i + 2], "little")
        i += 2
        akm = [_suite(val[i + 4 * k:i + 4 * k + 4], _RSN_AKM) for k in range(m)]
        i += 4 * m
        parts = [f"group={group}", f"pair={'/'.join(pair)}", f"akm={'/'.join(akm)}"]
        if len(val) >= i + 2:
            parts.append(f"caps={val[i:i + 2].hex()}")
        return "{" + ",".join(parts) + "}"
    except Exception:  # noqa: BLE001
        return _squeeze(val)


def _vendor(val: bytes) -> str:
    """Vendor Specific: OUI (with vendor name), vendor type (labeled if known), then the trimmed tail."""
    if len(val) < 4:
        return _squeeze(val)
    oui = ":".join(f"{b:02x}" for b in val[:3])
    name = VENDOR_BY_OUI.get(val[:3].hex().upper())
    label = _VENDOR_TYPE.get(bytes(val[:4]))
    head = f"oui={oui}" + (f"({name})" if name else "")
    vtype = f"type={val[3]}" + (f"({label})" if label else "")
    tail = _squeeze(val[4:])
    return "{" + head + "," + vtype + (f",data={tail}" if tail else "") + "}"


def _band(opclass: int) -> str:
    """The band an operating class sits in, or op<n> if it isn't one we map."""
    if opclass in (81, 82, 83, 84):
        return "2.4GHz"
    if 115 <= opclass <= 130:
        return "5GHz"
    if 131 <= opclass <= 137:
        return "6GHz"
    return f"op{opclass}"


def _rnr(val: bytes) -> str:
    """Reduced Neighbor Report: each neighbor AP info field summarized as band / channel."""
    try:
        out, i = [], 0
        while i + 4 <= len(val):
            count = ((val[i] >> 4) & 0x0F) + 1
            info_len = val[i + 1]
            if info_len == 0:
                break
            out.append(f"{_band(val[i + 2])}/ch{val[i + 3]}")
            i += 4 + count * info_len
        return f"({len(out)}: " + ",".join(out) + ")" if out else _squeeze(val)
    except Exception:  # noqa: BLE001
        return _squeeze(val)


def render_ie(tag: int, val: bytes) -> str | None:
    """One 'label=value' TLV entry, with light parsing and zero compression. None drops the IE
    (DSParam is only the channel: fluid, and already fixed by --channel)."""
    if tag == 3:
        return None
    if tag == 0:
        return f"SSID={_val(val)}"
    if tag in (1, 50):
        return f"{_IE_LABELS[tag]}={_rates(val)}"
    if tag == 48:
        return f"RSN={_rsn(val)}"
    if tag == 221:
        return f"Vendor={_vendor(val)}"
    if tag == 201:
        return f"RNR={_rnr(val)}"
    if tag == 255:
        label = _EXT_LABEL.get(val[0], f"Ext{val[0]}") if val else "Ext"
        return f"{label}={_squeeze(val[1:])}"
    return f"{_IE_LABELS.get(tag, f'IE{tag}')}={_squeeze(val)}"


def extract_features(pkt) -> list[str]:
    """Every fingerprint token for a frame, in order. The one place features are gathered: to add a
    field, call its extractor here. Returns [] when there is nothing past the frame name to show."""
    raw = pkt.raw or b""
    ftype = (raw[0] >> 2) & 0x03 if raw else 3
    subtype = (raw[0] >> 4) & 0x0F if raw else 0
    ies = parse_ies(raw, subtype) if ftype == 0 else []   # IEs live in mgmt frames only
    tokens = []
    entries = [e for e in (render_ie(t, v) for t, v in ies) if e]
    if entries:
        tokens.append("TLV{" + ",".join(entries) + "}")
    for token in (field_ieorder(ies), field_vendor(ies), field_wsc(ies)):
        if token:
            tokens.append(token)
    return tokens


def strings_of(raw: bytes, minlen: int) -> str:
    runs = re.findall(rb"[\x20-\x7e]{%d,}" % minlen, raw)
    return "STR{" + "|".join(r.decode("ascii") for r in runs) + "}" if runs else ""


def format_line(pkt, minlen: int, want_strings: bool) -> str | None:
    """One line for a frame, or None when nothing past the frame name is worth showing (so an empty
    keepalive-like frame is dropped rather than printed as a bare header)."""
    raw = pkt.raw or b""
    tokens = extract_features(pkt)
    if want_strings:
        strs = strings_of(raw, minlen)
        if strs:
            tokens.append(strs)
    if not tokens:
        return None
    return f"{pkt.source}->{pkt.dest} [{frame_name(raw)}] " + " ".join(tokens)


class Dumper:
    """RX callback: filter to a target MAC (if given), print one line, buffer for pcap (if given)."""

    def __init__(self, target: str, minlen: int, records, keep_all: bool = False,
                 want_strings: bool = False):
        self.target = target.lower() or None
        self.minlen = minlen
        self.records = records
        self.keep_all = keep_all
        self.want_strings = want_strings
        self.seen = 0      # frames matching the target filter
        self.shown = 0     # frames actually printed
        self._cw = 0       # width of the last counter written, to blank it before a frame line

    def __call__(self, pkt) -> None:
        if self.target and self.target not in (pkt.source, pkt.dest, pkt.bssid):
            return
        self.seen += 1
        if self.keep_all or frame_name(pkt.raw or b"") not in _NOISE:
            line = format_line(pkt, self.minlen, self.want_strings)
            if line is not None:
                self.shown += 1
                if self._cw:   # wipe the live counter off this line so the frame prints clean
                    sys.stderr.write("\r" + " " * self._cw + "\r")
                    sys.stderr.flush()
                print(line, flush=True)
                if self.records is not None and pkt.raw:
                    self.records.append((pkt.raw, time.time()))
        who = self.target or "all"
        msg = f"  {self.seen} frames to/from {who} ({self.shown} shown)"
        self._cw = len(msg)
        sys.stderr.write("\r" + msg)
        sys.stderr.flush()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", type=int, required=True, help="channel to tune to")
    ap.add_argument("--mac", default="", help="only frames whose source/dest/bssid == this MAC")
    ap.add_argument("--card", default="", help="adapter name/description substring (default: first)")
    ap.add_argument("--pcap", default="", help="also write captured frames to this .pcap on exit")
    ap.add_argument("--min-str", type=int, default=4, dest="min_str",
                    help="minimum length for printable-string extraction (default 4)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = run until Ctrl+C)")
    ap.add_argument("--all", dest="keep_all", action="store_true",
                    help="include null/keepalive frames (dropped as noise by default)")
    ap.add_argument("--strings", action="store_true",
                    help="emit STR{...} of printable byte runs (noisy, mostly ciphertext: off by default)")
    args = ap.parse_args()

    def err(m: str) -> None:
        print(m, file=sys.stderr, flush=True)

    err("[*] Discovering interfaces...")
    ifaces = wlan_ifaces()
    iface = select_device(ifaces, args.card)
    if iface is None:
        await wlan_close(ifaces)
        return 1

    err(f"[*] Bringing up {iface.description}...")
    try:
        if not await iface.connect(progress_cb=lambda p, m: err(f"  [{int(p * 100):3d}%] {m}")):
            await wlan_close(ifaces)
            return 1
    except Exception as e:  # noqa: BLE001
        err(f"[-] bring-up failed: {e}")
        await wlan_close(ifaces)
        return 1
    err(f"[*] MAC: {iface.mac_address}")

    if not await iface.set_channel(args.channel):
        err(f"[-] set_channel({args.channel}) failed")
        await wlan_close(ifaces)
        return 1
    err(f"[*] CH{args.channel}. Dumping"
        + (f" frames for {args.mac}" if args.mac else " all frames")
        + (f", pcap -> {args.pcap}" if args.pcap else "") + ". Ctrl+C to stop.")

    records = [] if args.pcap else None
    dumper = Dumper(args.mac, args.min_str, records, args.keep_all, args.strings)
    iface.register_rx_callback(dumper)
    try:
        if args.seconds > 0:
            await asyncio.sleep(args.seconds)
        else:
            while True:
                await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        iface.unregister_rx_callback(dumper)
        print(file=sys.stderr)   # end the live counter line
        err(f"[*] {dumper.shown} shown / {dumper.seen} seen"
            + (f" to/from {args.mac}" if args.mac else ""))
        if records:
            write_pcap(Path(args.pcap), records)
            err(f"[*] wrote {len(records)} frames to {args.pcap}")
        await wlan_close(ifaces)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted", file=sys.stderr)
        raise SystemExit(130)
