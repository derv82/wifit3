"""Run the Focus screen's WPS identity probe on every card x every AP, N times each, and tabulate.

Each run is what the Probe button does: probe_wps_implementation (broadcom-wps), then probe_ap
(wps_m1), on a WlanArray of one card, RUN_PAUSE_S after the last run on that AP. Every run is
appended to --results (TSV) tagged with the branch, commit and card, and a per-session log is
written next to it; the end prints the whole results file as a card@commit x AP matrix, so runs on
master and on a branch line up.

--sniffer names a card that is not probed: it follows each AP's channel and logs every frame
between the probing card's client MACs and the AP (no control frames, so no ACKs).

--aps lines are "<channel> <bssid> <label>"; '#' starts a comment.

Live TX against a network you do not own is illegal. Point this only at your own AP.

    uv run python scripts/campaigns/wps_probe.py --aps aps.txt --results results.tsv --sniffer 8822
    uv run python scripts/campaigns/wps_probe.py --aps aps.txt --results results.tsv --card 7925,5572
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent))

from dev import select_device
from wifit3.campaigns.probe import probe_ap
from wifit3.campaigns.probe.broadcom_wps import probe_wps_implementation
from wifit3.device.manager import wlan_ifaces, wlan_close
from wifit3.dot11 import mac_to_str, str_to_mac
from wifit3.dot11.packet import AssocRespPacket, AuthPacket, DeauthPacket, Packet
from wifit3.wlan.array import WlanArray

BEACON_WAIT_S = 5.0
RUN_PAUSE_S = 0.0
RESULT_COLUMNS = ("logged_at", "code", "card", "ap", "bssid", "channel", "run", "broadcom_wps",
                  "broadcom_wps_s", "result", "detail", "probe_s", "log_file")

log = logging.getLogger("wps_probe")
sniffer_log = logging.getLogger("sniffer")


@dataclass
class TestAp:
    channel: int
    bssid: str
    label: str


def read_aps(path: Path) -> list[TestAp]:
    aps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 3:
            aps.append(TestAp(int(fields[0]), fields[1].lower(), fields[2]))
    return aps


def code_version() -> str:
    """branch@commit, with +dirty when tracked files differ from the commit."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              cwd=_HERE).stdout.strip()
    version = f"{git('branch', '--show-current') or 'detached'}@{git('rev-parse', '--short', 'HEAD')}"
    if git("status", "--porcelain", "--untracked-files=no"):
        version += "+dirty"
    return version


class Sniffer:
    """Logs frames between an AP and every client MAC the probing cards transmit as."""

    def __init__(self):
        self.bssid = b""
        self.client_macs: set[bytes] = set()

    def record_tx(self, frame: bytes) -> None:
        if len(frame) >= 16 and frame[4:10] == self.bssid:
            self.client_macs.add(bytes(frame[10:16]))

    def on_frame(self, pkt: Packet) -> None:
        frame = pkt.raw
        if len(frame) < 24:
            return
        addr1 = bytes(frame[4:10])
        addr2 = bytes(frame[10:16])
        if addr1 == self.bssid and addr2 in self.client_macs:
            direction = "client -> AP"
            client = addr2
        elif addr2 == self.bssid and addr1 in self.client_macs:
            direction = "AP -> client"
            client = addr1
        else:
            return
        retry = " retry" if frame[1] & 0x08 else ""
        seq = int.from_bytes(frame[22:24], "little") >> 4
        detail = ""
        if isinstance(pkt, (AuthPacket, AssocRespPacket)):
            detail = f" status {pkt.status}"
        elif isinstance(pkt, DeauthPacket):
            detail = f" reason {pkt.reason}"
        sniffer_log.info("%s %s %-10s seq %4d%s%s rssi %d", direction, mac_to_str(client),
                         pkt.type, seq, retry, detail, pkt.rssi)


class Results:
    def __init__(self, path: Path, code: str, log_file: str):
        self.path = path
        self.code = code
        self.log_file = log_file

    def append(self, card: str, ap: TestAp, run: int, broadcom_wps: str, broadcom_wps_s: float,
               result: str, detail: str, probe_s: float) -> None:
        new_file = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter="\t")
            if new_file:
                writer.writerow(RESULT_COLUMNS)
            writer.writerow((datetime.now().isoformat(timespec="seconds"), self.code, card,
                             ap.label, ap.bssid, ap.channel, run, broadcom_wps,
                             f"{broadcom_wps_s:.2f}", result, detail, f"{probe_s:.2f}",
                             self.log_file))


def print_matrix(path: Path) -> None:
    """One row per card@code, one column per AP: matched/runs, or the skip reason."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    aps = list(dict.fromkeys(row["ap"] for row in rows))
    lines = list(dict.fromkeys(f"{row['card']} {row['code']}" for row in rows))
    cells: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        cells.setdefault((f"{row['card']} {row['code']}", row["ap"]), []).append(row["result"])
    line_width = max(len(line) for line in lines)
    widths = [max(len(ap), 8) for ap in aps]
    print()
    print(" " * line_width + "  " + "  ".join(ap.ljust(w) for ap, w in zip(aps, widths)))
    for line in lines:
        texts = []
        for ap in aps:
            results = cells.get((line, ap), [])
            runs = [r for r in results if r in ("matched", "failed")]
            if runs:
                texts.append(f"{runs.count('matched')}/{len(runs)}")
            elif results:
                texts.append(results[-1])
            else:
                texts.append("")
        print(line.ljust(line_width) + "  " + "  ".join(t.ljust(w) for t, w in zip(texts, widths)))


async def probe_card(iface, aps: list[TestAp], runs: int, sniffer_iface, sniffer: Sniffer,
                     results: Results) -> None:
    array = WlanArray()
    array.attach(iface)
    array_record_tx = iface.on_tx

    def record_tx(frame: bytes) -> None:
        sniffer.record_tx(frame)
        array_record_tx(frame)

    iface.on_tx = record_tx
    card = iface.description
    for test_ap in aps:
        log.info("=== %s / %s (%s, ch %d)", card, test_ap.label, test_ap.bssid, test_ap.channel)
        if test_ap.channel not in iface.supported_channels:
            log.info("[-] %s can't tune ch %d", card, test_ap.channel)
            results.append(card, test_ap, 0, "", 0.0, "no channel", "", 0.0)
            continue
        sniffer.bssid = str_to_mac(test_ap.bssid)
        if sniffer_iface is not None and test_ap.channel in sniffer_iface.supported_channels:
            await sniffer_iface.set_channel(test_ap.channel)
        await iface.set_channel(test_ap.channel)
        deadline = time.monotonic() + BEACON_WAIT_S
        while test_ap.bssid not in array.access_points and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        ap = array.access_points.get(test_ap.bssid)
        if ap is None:
            log.info("[-] no beacon from %s in %.0fs", test_ap.bssid, BEACON_WAIT_S)
            results.append(card, test_ap, 0, "", 0.0, "not seen", "", 0.0)
            continue
        if not ap.wps:
            log.info("[-] %s beacons no WPS IE", test_ap.label)
            results.append(card, test_ap, 0, "", 0.0, "no wps", "", 0.0)
            continue
        log.info("[*] ssid=%r wps=%s beacons=%d", ap.ssid, ap.wps_version, ap.beacons)
        for n in range(1, runs + 1):
            if n > 1:
                await asyncio.sleep(RUN_PAUSE_S)
            log.info("--- %s / %s run %d", card, test_ap.label, n)
            async with array.claim(iface):
                start = time.monotonic()
                implementation = await probe_wps_implementation(iface, ap)
                implementation_s = time.monotonic() - start
                result = await probe_ap(iface, ap)
                probe_s = time.monotonic() - start - implementation_s
            outcome = "matched" if result.ok else "failed"
            detail = ap.identity.summary if result.ok else (result.detail or "")
            log.info("[%s] %s / %s run %d: broadcom-wps %s in %.2fs; probe %s (%s) in %.2fs",
                     "+" if result.ok else "-", card, test_ap.label, n, implementation.name,
                     implementation_s, outcome, detail, probe_s)
            results.append(card, test_ap, n, implementation.name, implementation_s, outcome,
                           detail, probe_s)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aps", type=Path, help="AP list file; or one AP via --bssid + --channel")
    parser.add_argument("--bssid", help="one AP to probe")
    parser.add_argument("--channel", type=int, help="that AP's channel")
    parser.add_argument("--card", default="",
                        help="comma-separated probing adapter substrings, e.g. 7925,5572 "
                             "(default: every card but the sniffer)")
    parser.add_argument("--sniffer", default="", help="sniffing adapter substring, e.g. 8822")
    parser.add_argument("--runs", type=int, default=5, help="probes per card per AP")
    parser.add_argument("--results", type=Path, required=True,
                        help="TSV to append to; the session log goes next to it")
    args = parser.parse_args()
    if args.aps:
        aps = read_aps(args.aps)
    elif args.bssid and args.channel:
        aps = [TestAp(args.channel, args.bssid.lower(), args.bssid.lower())]
    else:
        parser.error("give --aps, or --bssid and --channel")

    code = code_version()
    log_path = args.results.parent / (f"wps-probe-{datetime.now():%Y%m%d-%H%M%S}-"
                                      f"{code.replace('@', '-').replace('/', '-')}.log")
    log_format = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
                                   datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    log_file = logging.FileHandler(log_path, encoding="utf-8")
    for handler in (console, log_file):
        handler.setFormatter(log_format)
    logging.basicConfig(level=logging.INFO, handlers=[console, log_file])
    log.info("code %s, log %s", code, log_path)
    results = Results(args.results, code, log_path.name)

    ifaces = wlan_ifaces()
    sniffer_iface = select_device(ifaces, args.sniffer) if args.sniffer else None
    if args.sniffer and sniffer_iface is None:
        await wlan_close(ifaces)
        return 1
    if args.card:
        cards = [select_device(ifaces, substring) for substring in args.card.split(",")]
        if None in cards:
            await wlan_close(ifaces)
            return 1
    else:
        cards = [i for i in ifaces if i is not sniffer_iface]
    if sniffer_iface in cards:
        log.info("[-] %s is both a probing card and the sniffer", sniffer_iface.description)
        await wlan_close(ifaces)
        return 1

    sniffer = Sniffer()
    try:
        if sniffer_iface is not None:
            if not await sniffer_iface.connect():
                log.info("[-] sniffer %s bring-up failed", sniffer_iface.description)
                return 1
            sniffer_iface.register_rx_callback(sniffer.on_frame)
            log.info("[*] sniffer: %s", sniffer_iface.description)
        for iface in cards:
            try:
                connected = await iface.connect()
            except Exception as exc:
                log.info("[-] %s bring-up failed: %s", iface.description, exc)
                continue
            if not connected:
                log.info("[-] %s bring-up failed", iface.description)
                continue
            await probe_card(iface, aps, args.runs, sniffer_iface, sniffer, results)
    finally:
        await wlan_close(ifaces)
    if args.results.exists():
        print_matrix(args.results)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        raise SystemExit(130)
