"""scan: an 802.11 diff detector. Dedup the beacon spam, print only what is new or changed.

Brings up one wifit3 USB interface, tunes to a channel, and taps the raw RX stream. Each frame is
parsed by frame.Frame; a Tracker accumulates the decoded IEs per (source, frame type) and emits a
record only on first sight or when an IE was added or changed. An identical repeat frame emits
nothing: that is the dedup.

Passive by default (RX only). With --wps (which needs --mac), it also, once, 5s after start,
associates to that BSSID as an external WPS registrar and prints the AP's WSC M1 device attributes
(manufacturer, model, device name, ...). That path transmits (auth/assoc + EAPOL).

Records go to stdout, a live counter and status to stderr, so a plain redirect keeps output clean:

    uv run python scripts/id/scan.py --channel 6 > diffs.txt
    uv run python scripts/id/scan.py --channel 6 --mac aa:bb:cc:dd:ee:ff --seconds 30
    uv run python scripts/id/scan.py --channel 1 --mac aa:bb:cc:dd:ee:ff --wps
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py
sys.path.insert(0, str(_HERE))         # scripts/id/ for frame.py

from dev import select_device
from frame import Frame, IE, fmt
from wifit3.campaigns.auth_assoc import Association, WlanTransport, random_client_mac
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc import messages as M
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie


@dataclass
class Record:
    """What the Tracker emits for one frame. kind is 'new' (full ies) or 'diff' (added / changed)."""
    kind: str
    src: str
    name: str
    ies: dict = field(default_factory=dict)      # full IE map, for a 'new' record
    added: dict = field(default_factory=dict)    # IEs not seen before, for a 'diff'
    changed: dict = field(default_factory=dict)  # IE -> (old, new), for a 'diff'


class Tracker:
    """Accumulates decoded IEs per (source, frame name) and returns a Record only on a real change.

    Pure logic: observe(frame) returns a Record or None, it never prints. Removals are not reported:
    a frame simply not carrying an IE this time is not a change, and stored IEs only accumulate."""

    def __init__(self, mac_filter: str = ""):
        self.mac = mac_filter.lower() or None
        self.stored: dict[tuple[str, str], dict] = {}
        self.emitted = 0

    def observe(self, frame: Frame | None) -> Record | None:
        """Fold one parsed frame into the accumulated state, returning a Record when it is new or
        changed, else None. Frames that are None, carry no IEs, or miss the MAC filter are ignored."""
        if frame is None or not frame.ies:
            return None
        if self.mac and self.mac not in (frame.src, frame.dst):
            return None
        key = (frame.src, frame.name)
        stored = self.stored.get(key)
        if stored is None:
            self.stored[key] = dict(frame.ies)
            self.emitted += 1
            return Record("new", frame.src, frame.name, ies=dict(frame.ies))
        added = {ie: v for ie, v in frame.ies.items() if ie not in stored}
        changed = {ie: (stored[ie], v) for ie, v in frame.ies.items()
                   if ie in stored and stored[ie] != v}
        merged = dict(frame.ies)
        if IE.SSID in changed and frame.ies[IE.SSID] == b"":
            del changed[IE.SSID]        # a hidden AP flapping SSID to blank is noise, keep the name
            merged.pop(IE.SSID, None)
        stored.update(merged)
        if not added and not changed:
            return None
        self.emitted += 1
        return Record("diff", frame.src, frame.name, added=added, changed=changed)

    @property
    def devices(self) -> int:
        """Distinct sources tracked so far."""
        return len({src for src, _ in self.stored})


def format_record(rec: Record) -> str:
    """A Record as its human readable block: a header line plus one indented line per IE."""
    if rec.kind == "new":
        lines = [f"NEW {rec.src} [{rec.name}]"]
        lines += [f"    {ie.label}={fmt(v)}" for ie, v in rec.ies.items()]
        return "\n".join(lines)
    lines = [f"~ {rec.src} [{rec.name}]"]
    lines += [f"    + {ie.label}={fmt(v)}" for ie, v in rec.added.items()]
    lines += [f"    * {ie.label}: {fmt(old)} -> {fmt(new)}"
              for ie, (old, new) in rec.changed.items()]
    return "\n".join(lines)


class Scanner:
    """RX callback: parse each frame, feed the Tracker, print a Record when one is emitted."""

    def __init__(self, tracker: Tracker, sink=print):
        self.tracker = tracker
        self.sink = sink
        self.seen = 0
        self._cw = 0   # width of the last counter written, to blank it before a record

    def __call__(self, pkt) -> None:
        self.seen += 1
        rec = self.tracker.observe(Frame.parse(pkt.raw or b""))
        if rec is not None:
            if self._cw:   # wipe the live counter off this line so the record prints clean
                sys.stderr.write("\r" + " " * self._cw + "\r")
                sys.stderr.flush()
            self.sink(format_record(rec))
        if self.seen % 111 == 0:   # throttle: rewriting per frame lags the console on busy channels
            msg = f"  {self.seen} frames, {self.tracker.devices} devices, {self.tracker.emitted} records"
            self._cw = len(msg)
            sys.stderr.write("\r" + msg + "\r")
            sys.stderr.flush()


# ======================================================================================
# WPS active probe: associate as an external registrar and read the AP's M1. M1 is the first WSC
# message the AP (enrollee) sends us, so it needs no PIN and no crypto. Only used with --wps.
# ======================================================================================
# WSC M1 attribute id -> short label, for the device identity / capability fields worth surfacing.
_WSC_M1_FIELDS = {
    M.ATTR_MANUFACTURER: "mfr", M.ATTR_MODEL_NAME: "model", M.ATTR_MODEL_NUMBER: "model_no",
    M.ATTR_DEV_NAME: "name", M.ATTR_SERIAL_NUMBER: "serial", M.ATTR_UUID_E: "uuid",
    M.ATTR_PRIMARY_DEV_TYPE: "dev_type", M.ATTR_OS_VERSION: "os_version",
    M.ATTR_RF_BANDS: "rf_bands", M.ATTR_CONFIG_METHODS: "config_methods",
    M.ATTR_WPS_STATE: "wps_state", M.ATTR_VERSION: "version",
}


def _macs(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _wsc_val(val: bytes) -> str:
    """A WSC attribute value as text when fully printable, else hex."""
    if val and all(0x20 <= b <= 0x7e for b in val):
        return val.decode("ascii")
    return val.hex()


def format_wps_m1(bssid: str, attrs: dict) -> str:
    """The surfaced M1 attributes as one line: `WPS-M1 <bssid> mfr=..., model=..., ...`."""
    fields = [f"{label}={_wsc_val(attrs[aid])}"
              for aid, label in _WSC_M1_FIELDS.items() if aid in attrs]
    return f"WPS-M1 {bssid} " + ", ".join(fields)


def _ssid_for(tracker: Tracker, mac: str) -> str:
    """The SSID the tracker has seen for `mac` (beacon then probe_resp), or '' if none yet."""
    mac = mac.lower()
    for name in ("beacon", "probe_resp"):
        ies = tracker.stored.get((mac, name))
        if ies and ies.get(IE.SSID):
            return ies[IE.SSID].decode("utf-8", "replace")
    return ""


async def _harvest_m1(transport, bssid: bytes, our_mac: bytes,
                      tries: int = 10, timeout: float = 3.0) -> dict | None:
    """Send EAPOL-Start, answer the AP's EAP identity request, and return the WSC attributes of the
    first M1 it sends, or None if none arrives. Resends the last frame on a silent window (our
    injected frames land no-ACK, so a lost frame otherwise stalls the exchange)."""
    start = M.build_data_frame(bssid, our_mac, bssid, M.eapol_start())
    await transport.send_no_wait(start)
    last = start
    for _ in range(tries):
        frame = await transport.recv(timeout)
        if frame is None:
            await transport.send_no_wait(last)
            continue
        p = M.parse_rx_frame(frame)
        if p is None:
            continue
        if p.is_identity_request:
            last = M.build_data_frame(bssid, our_mac, bssid, M.eap_identity_response(p.eap_id))
            await transport.send_no_wait(last)
        elif p.wsc_msg_type == M.WPS_M1:
            return p.attrs
    return None


async def wps_probe_m1(iface, bssid: str, ssid: str, channel: int, err, sink=print) -> None:
    """Associate to `bssid` as an external registrar (active monitor for auto-ACK) and print its WSC
    M1 device attributes. One shot: it transmits auth/assoc + EAPOL, then leaves the BSS."""
    bssid_b = str_to_mac(bssid)
    our_mac = random_client_mac()
    armed = await iface.set_fake_mac(our_mac, bssid_b)   # active monitor: chip HW-ACKs AP -> us
    if armed:                                            # FIXED_MAC returns the silicon MAC
        our_mac = str_to_mac(armed)
    try:
        await iface.enable_rx_acks()
    except Exception as e:  # noqa: BLE001
        err(f"\r[wps] enable_rx_acks failed (continuing): {e}")
    assoc = Association(iface, bssid, ssid, channel, our_mac=our_mac,
                        assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR))
    transport = WlanTransport(iface, bssid_b, our_mac)
    assoc.start()
    try:
        if not await assoc.associate():
            err(f"\r[wps] not associated to {bssid}: {assoc.fail_reason or 'no response'}")
            return
        err(f"\r[wps] associated to {bssid} as {_macs(our_mac)}; driving WSC to M1...")
        transport.start()
        attrs = await _harvest_m1(transport, bssid_b, our_mac)
    finally:
        transport.stop()
        assoc.stop()
        try:
            await iface.clear_fake_mac()
        except Exception:  # noqa: BLE001
            pass
    if attrs is None:
        err(f"\r[wps] no M1 from {bssid} (AP may not allow an external registrar, or WPS is locked)")
        return
    sink(format_wps_m1(bssid, attrs))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", type=int, required=True, help="channel to tune to")
    ap.add_argument("--mac", default="", help="only frames whose source/dest == this MAC")
    ap.add_argument("--card", default="", help="adapter name/description substring (default: first)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = run until Ctrl+C)")
    ap.add_argument("--wps", action="store_true",
                    help="5s after start, associate to --mac as a WPS registrar and print its M1 "
                         "device info (transmits; requires --mac)")
    args = ap.parse_args()
    if args.wps and not args.mac:
        ap.error("--wps requires --mac (the target BSSID)")

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
    err(f"[*] CH{args.channel}. Scanning"
        + (f" frames for {args.mac}" if args.mac else "") + ". Ctrl+C to stop.")

    scanner = Scanner(Tracker(args.mac))
    iface.register_rx_callback(scanner)

    async def _run_wps() -> None:
        await asyncio.sleep(5.0)          # let the passive tap learn the SSID first
        ssid = _ssid_for(scanner.tracker, args.mac)
        err(f"\r[wps] probing {args.mac} (ssid={ssid!r}) as external registrar (transmits)...")
        try:
            await wps_probe_m1(iface, args.mac, ssid, args.channel, err)
        except Exception as e:  # noqa: BLE001
            err(f"\r[wps] error: {e}")

    wps_task = asyncio.create_task(_run_wps()) if args.wps else None
    try:
        if args.seconds > 0:
            await asyncio.sleep(args.seconds)
        else:
            while True:
                await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if wps_task is not None and not wps_task.done():
            wps_task.cancel()
        iface.unregister_rx_callback(scanner)
        print(file=sys.stderr)   # end the live counter line
        err(f"[*] {scanner.tracker.emitted} records / {scanner.seen} frames"
            + f", {scanner.tracker.devices} devices")
        await wlan_close(ifaces)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted", file=sys.stderr)
        raise SystemExit(130)
