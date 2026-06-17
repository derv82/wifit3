"""Raw-RX saturation probe — the diagnostic the driver's RX path hides.

`iter_frames` drops crc/icv-error frames before the callback, so a front-end
*saturating* on the near reference AP (strong signal clips -> beacons fail CRC ->
dropped) looks identical to "AP not heard" from the beacon_watch level. This probe
does its own bring-up (no competing reader on the bulk-IN pipe) and decodes EVERY
RX descriptor, recording per-BSSID:

  * total descriptors seen, crc-error count, icv-error count
  * the raw OFDM pwdb_all byte (phy_status[4]) — rails to ~253 when the AGC gain is
    set too high (the saturation signature the runbook names)
  * decoded RSSI in dBm

Pin the 2.4 GHz reference BSSID; the top "loud far" APs come along for contrast. If
the reference AP shows a railed pwdb + a high crc-error fraction while far APs decode
clean, the front end is saturated -> chase init RX gain. If the reference AP's
descriptors simply never arrive, it is not saturation.

    uv run python scripts/rtl8814au_dkms/rx_saturation_probe.py --bssid <ref> --duration 30

No files written; BSSID is a runtime arg only.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import libusb_package  # noqa: E402
import usb.core  # noqa: E402

from wifit3.chips.rtl8814au_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8814au_dkms.bb import phy_bb_config  # noqa: E402
from wifit3.chips.rtl8814au_dkms.chan import init_tune, set_channel_bw, set_rfe_reg_init  # noqa: E402
from wifit3.chips.rtl8814au_dkms.dm import init_hal_dm  # noqa: E402
from wifit3.chips.rtl8814au_dkms.efuse import read_chip_params  # noqa: E402
from wifit3.chips.rtl8814au_dkms.firmware import bring_up  # noqa: E402
from wifit3.chips.rtl8814au_dkms.mac import hal_init_turn_on, mac_init_misc, phy_mac_config  # noqa: E402
from wifit3.chips.rtl8814au_dkms.monitor import enable_rx_bar, enter_monitor, set_sta_opmode  # noqa: E402
from wifit3.chips.rtl8814au_dkms.rf import phy_rf_config  # noqa: E402
from wifit3.chips.rtl8814au_dkms.rx import RXDESC_SIZE, decode_rssi, iter_frames, query_rx_desc  # noqa: E402
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402
from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport  # noqa: E402
from wifit3.chips.rtl8814au_dkms.watchdog import WATCHDOG_PERIOD_S, WatchdogState  # noqa: E402
from wifit3.chips.rtl8814au_dkms.watchdog import tick as watchdog_tick  # noqa: E402

FW_BIN = REPO / "src" / "wifit3" / "chips" / "rtl8814au_dkms" / "assets" / "rtl8814au_fw.bin"


def _find_dev():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=C.VID_REALTEK, idProduct=C.PID_RTL8814AU, backend=backend)
    if dev is None:
        print("[-] RTL8814AU (0bda:8813) not found.", file=sys.stderr)
        raise SystemExit(1)
    return dev


def _bring_up(t, channel: int) -> int:
    fw = FW_BIN.read_bytes()
    params = read_chip_params(t)
    print(f"[*] efuse: rfe_type={params.rfe_type} crystal_cap=0x{params.crystal_cap:02x} "
          f"chip_version=0x{params.chip_version:08x}", file=sys.stderr)
    if not bring_up(t, fw):
        print("[-] firmware not ready", file=sys.stderr)
        raise SystemExit(1)
    phy_mac_config(t)
    mac_init_misc(t)
    phy_bb_config(t, params.rfe_type, params.crystal_cap)
    phy_rf_config(t, params.rfe_type)
    init_tune(t, channel, params.tx_power, params.tx_power_5g, params.bb_swing, params.bb_swing_5g)
    igi_seed = init_hal_dm(t)
    set_rfe_reg_init(t, params.rfe_type)
    hal_init_turn_on(t, params.mac_address)
    enable_rx_bar(t)
    set_channel_bw(t, channel, params.tx_power, params.tx_power_5g,
                   params.bb_swing, params.bb_swing_5g, current_band=C.BAND_MAX)
    set_sta_opmode(t, params.mac_address)
    enter_monitor(t)
    print(f"[*] DIG IGI seed = 0x{igi_seed:02x}", file=sys.stderr)
    return igi_seed


def _bssid_of_beacon(frame: bytes) -> str | None:
    """addr3 (BSSID) of a BEACON mgmt frame (subtype 8), else None.

    Beacon-only (not probe-resp) to match the kernel target (beacon_watch_usbcap's
    ``8000`` regex) and beacon_watch's ``type=='beacon'`` — an apples-to-apples count.
    """
    if len(frame) < 24:
        return None
    fc = frame[0]
    # type=mgmt (00), subtype beacon(8). fc byte = subtype<<4 | type<<2 | ver.
    if (fc & 0xFC) != 0x80:
        return None
    return ":".join(f"{b:02x}" for b in frame[16:22])


class Stat:
    __slots__ = ("descs", "crc", "icv", "pwdb", "rssi")

    def __init__(self):
        self.descs = 0
        self.crc = 0
        self.icv = 0
        self.pwdb: list[int] = []
        self.rssi: list[int] = []


def _walk_raw(buf: bytes, stats: dict):
    """Decode every descriptor in an aggregated bulk-IN buffer (incl. crc/icv errors)."""
    off, n = 0, len(buf)
    while n - off >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or off + pkt_offset > n:
            break
        if not d.rpt_sel:
            start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
            frame = buf[start:start + d.pkt_len]
            bssid = _bssid_of_beacon(frame)
            if bssid is not None:
                s = stats[bssid]
                s.descs += 1
                if d.crc_err:
                    s.crc += 1
                if d.icv_err:
                    s.icv += 1
                if d.physt:
                    phy = buf[off + RXDESC_SIZE:start]
                    if len(phy) >= 6:
                        s.pwdb.append(phy[4])
                        s.rssi.append(decode_rssi(phy, d.data_rate))
        off += (pkt_offset + 7) & ~7
    return


_FRAME_KIND = Counter()   # iter_frames output: 802.11 type breakdown (mgmt/ctrl/data/other)
_FRAME_SIZE = Counter()   # frame-size histogram (bucketed)
_MAX_PER_BUF = [0]        # fattest single buffer (frames yielded)
_FAT_BUF = [b""]          # a sample fat buffer for hex inspection
_BAD_VER = [0]            # frames whose FC protocol-version bits != 0 (real 802.11 = 0 => garbage)
_GARBAGE_BUFS = [0]       # buffers that yielded a 'bad-version' frame (a derailed walk)


def _walk_parsed(buf: bytes, ref: str) -> int:
    """Driver-equivalent dispatch: iter_frames -> WlanFrameParser. Count ref-AP beacons the
    real driver path would deliver, and characterize EVERY frame iter_frames yields. A real
    802.11 frame has protocol-version bits (FC[1:0]) == 0; a non-zero version means iter_frames
    walked off a frame boundary and is emitting garbage (also losing that buffer's real beacons)."""
    n = 0
    per_buf = 0
    bad_here = False
    for frame, rssi in iter_frames(buf):
        per_buf += 1
        fc = frame[0] if frame else 0
        if fc & 0x03:                # protocol version != 0 -> not a real 802.11 frame
            _BAD_VER[0] += 1
            bad_here = True
        ftype = (fc & 0x0C) >> 2     # 0=mgmt 1=ctrl 2=data 3=ext
        _FRAME_KIND[("mgmt", "ctrl", "data", "ext")[ftype]] += 1
        _FRAME_SIZE[min(len(frame) // 16 * 16, 256)] += 1
        parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
        if parsed and parsed.get("type") == "beacon" and (parsed.get("bssid") or "").lower() == ref:
            n += 1
    if bad_here:
        _GARBAGE_BUFS[0] += 1
    if per_buf > _MAX_PER_BUF[0]:
        _MAX_PER_BUF[0] = per_buf
        _FAT_BUF[0] = buf
    return n


def _trace_walk(buf: bytes, limit: int = 40) -> None:
    """Dump the descriptor-walk of one buffer step-by-step (to see where it derails)."""
    off, n = 0, len(buf)
    step = 0
    print(f"\n[*] walk trace of fattest buffer ({n} bytes):", file=sys.stderr)
    while n - off >= RXDESC_SIZE and step < limit:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
        fc = buf[start] if start < n else 0
        bad = " <-- pkt_offset>buf" if (d.pkt_len <= 0 or off + pkt_offset > n) else ""
        ver = fc & 0x03
        print(f"  off={off:>6} pkt_len={d.pkt_len:>5} drvinfo={d.drvinfo_sz:>3} "
              f"shift={d.shift_sz} physt={int(d.physt)} crc={int(d.crc_err)} "
              f"fc=0x{fc:02x}(ver{ver}) -> adv {(pkt_offset + 7) & ~7}{bad}", file=sys.stderr)
        if d.pkt_len <= 0 or off + pkt_offset > n:
            break
        off += (pkt_offset + 7) & ~7
        step += 1


def _multi_reader(t, ref: str, channel: int, duration: float, n_threads: int) -> int:
    """N concurrent bulk-IN reader threads, each decoding ref-AP beacons independently.

    Tests whether keeping more reads in flight drains the chip RX FIFO faster (the kernel
    keeps multiple URBs posted; our single sync read loop has a gap between each read). Each
    thread reads + counts ref beacons into its own Stat (no shared lock on the hot path)."""
    import threading
    t._bulk_in_ep()                       # prime the cached endpoint probe before the race
    print(f"[*] watching ch{channel} for {duration:g}s with {n_threads} reader threads...",
          file=sys.stderr)
    stop = threading.Event()
    results: list = [None] * n_threads

    def worker(idx: int) -> None:
        st: dict = defaultdict(Stat)
        nbuf = nbytes = 0
        while not stop.is_set():
            try:
                buf = t.bulk_in()
            except Exception as e:  # noqa: BLE001
                print(f"  [thread {idx}] read error: {e}", file=sys.stderr)
                break
            if buf:
                nbuf += 1
                nbytes += len(buf)
                _walk_raw(buf, st)
            s = st[ref]
            results[idx] = (s.descs - s.crc - s.icv, nbuf, nbytes)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_threads)]
    start = time.monotonic()
    for th in threads:
        th.start()
    while time.monotonic() - start < duration:
        time.sleep(0.2)
    stop.set()
    for th in threads:
        th.join(timeout=2.0)
    elapsed = time.monotonic() - start
    t.close()

    tot_bcn = sum(r[0] for r in results if r)
    tot_buf = sum(r[1] for r in results if r)
    tot_bytes = sum(r[2] for r in results if r)
    print(f"\n[*] {n_threads} threads, {elapsed:.0f}s: ref-AP good beacons = {tot_bcn} "
          f"({tot_bcn / elapsed:.1f}/s)", file=sys.stderr)
    print(f"[*] aggregate drain: {tot_buf} buffers ({tot_buf / elapsed:.0f}/s), "
          f"{tot_bytes / elapsed / 1024:.0f} KiB/s", file=sys.stderr)
    print(f"[*] per-thread ref beacons: {[r[0] if r else 0 for r in results]}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Raw-RX per-BSSID saturation probe (incl. CRC errors).")
    ap.add_argument("--bssid", required=True, help="reference BSSID to highlight")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--dig", action="store_true",
                    help="run the phydm watchdog tick inline every 2s (isolate its RX effect)")
    ap.add_argument("--fast", action="store_true",
                    help="skip the per-frame full parse/characterization (minimal per-buffer work) "
                         "— measures the drain-limited beacon ceiling vs the heavy-parse path")
    ap.add_argument("--threads", type=int, default=1,
                    help="N concurrent bulk-IN reader threads (keep more reads in flight to drain "
                         "the chip RX FIFO faster). >1 uses the multi-reader measurement path.")
    args = ap.parse_args()
    ref = args.bssid.lower()

    dev = _find_dev()
    t = Rtl8814auTransport(dev)
    print(f"[*] bringing up on ch{args.channel}...", file=sys.stderr)
    igi_seed = _bring_up(t, args.channel)

    if args.threads > 1:
        return _multi_reader(t, ref, args.channel, args.duration, args.threads)

    print(f"[*] watching ch{args.channel} for {args.duration:g}s (raw descriptors)"
          f"{' + inline DIG watchdog' if args.dig else ''}...", file=sys.stderr)

    stats: dict = defaultdict(Stat)
    st = WatchdogState(cur_ig_value=igi_seed, nhm_igi=igi_seed)
    # Per-10s ref-AP good-beacon series, to expose runtime decay.
    win_sz = 10.0
    ref_win: dict = defaultdict(int)
    parser_ref = 0  # ref-AP beacons the iter_frames+WlanFrameParser path would deliver
    n_buf = n_bytes = 0  # bulk-IN throughput: does the chip DELIVER the beacons at all? (bucket c)
    start = time.monotonic()
    next_tick = start + WATCHDOG_PERIOD_S
    while True:
        now = time.monotonic()
        if now - start >= args.duration:
            break
        if args.dig and now >= next_tick:
            fa = watchdog_tick(t, st)
            print(f"  [t={now - start:4.0f}s] DIG tick: IGI=0x{st.cur_ig_value:02x} fa={fa}",
                  file=sys.stderr)
            next_tick += WATCHDOG_PERIOD_S
        buf = t.bulk_in()
        if buf:
            n_buf += 1
            n_bytes += len(buf)
            before = stats[ref].descs - stats[ref].crc - stats[ref].icv
            _walk_raw(buf, stats)
            after = stats[ref].descs - stats[ref].crc - stats[ref].icv
            ref_win[int((time.monotonic() - start) // win_sz)] += (after - before)
            if not args.fast:
                parser_ref += _walk_parsed(buf, ref)
    elapsed = time.monotonic() - start
    t.close()
    print(f"\n[*] bulk-IN throughput: {n_buf} buffers ({n_buf / elapsed:.0f}/s), "
          f"{n_bytes} bytes ({n_bytes / elapsed / 1024:.0f} KiB/s) — fast tight-loop drain",
          file=sys.stderr)
    print(f"[*] ref-AP BEACONS: raw-desc={stats[ref].descs - stats[ref].crc - stats[ref].icv} "
          f"vs iter_frames+parser={parser_ref}", file=sys.stderr)
    total_frames = sum(_FRAME_KIND.values())
    print(f"[*] iter_frames yielded {total_frames} frames ({total_frames / elapsed:.0f}/s); "
          f"type breakdown: {dict(_FRAME_KIND)}", file=sys.stderr)
    print(f"[*] frame-size histogram (bucket->count): "
          f"{dict(sorted(_FRAME_SIZE.items()))}", file=sys.stderr)
    print(f"[*] fattest single buffer yielded {_MAX_PER_BUF[0]} frames "
          f"({len(_FAT_BUF[0])} bytes)", file=sys.stderr)
    print(f"[*] GARBAGE: {_BAD_VER[0]} frames had FC version != 0 (of {total_frames}); "
          f"{_GARBAGE_BUFS[0]} buffers derailed", file=sys.stderr)
    if _FAT_BUF[0]:
        _trace_walk(_FAT_BUF[0])

    print(f"\n[*] ref-AP GOOD beacons per {win_sz:g}s window:")
    for w in sorted(ref_win):
        n = ref_win[w]
        print(f"  [{w * 10:>3}-{w * 10 + 10:>3}s] {n:>4} good ({n / win_sz:.1f}/s)  {'#' * n}")

    def fmt(b, s: Stat) -> str:
        good = s.descs - s.crc - s.icv
        crc_pct = 100.0 * s.crc / s.descs if s.descs else 0.0
        pwdb_mean = sum(s.pwdb) / len(s.pwdb) if s.pwdb else 0
        pwdb_max = max(s.pwdb) if s.pwdb else 0
        rssi_mean = sum(s.rssi) / len(s.rssi) if s.rssi else 0
        mark = "  <-- REF" if b == ref else ""
        return (f"  {b}  descs={s.descs:>5} good={good:>5} crc={s.crc:>5}({crc_pct:>4.0f}%) "
                f"icv={s.icv:>4} pwdb(mean/max)={pwdb_mean:>5.0f}/{pwdb_max:>3} "
                f"rssi~{rssi_mean:>5.0f}dBm{mark}")

    print(f"\n[*] per-BSSID over {args.duration:g}s on ch{args.channel}:")
    ordered = sorted(stats.items(), key=lambda kv: kv[1].descs, reverse=True)
    for b, s in ordered[:15]:
        print(fmt(b, s))
    if ref not in stats:
        print(f"\n[!] reference {ref} produced ZERO descriptors (not even crc-error).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] interrupted.", file=sys.stderr)
        sys.exit(130)
