"""RT5372 RX diagnostic — where do beacons go on a fixed channel?

A richer RX check than ``beacon_watch.py``: brings the card up (cold), tunes a fixed
channel, then runs its OWN bulk-IN read loop (not the driver's reader thread) and tallies
EVERY aggregated frame — including the ones ``rx.iter_frames`` would silently drop — so a
beacon shortfall can be localized to one of three layers:

  * USB throughput   — reads/s, bytes/read (is it pinned at the 16 KB cap = backpressure?),
                       timeouts. If we're read-bound the chip's RX-DMA overruns between reads.
  * CRC errors       — frames the chip flags RXD_W0_CRC_ERROR (we discard them). A high CRC
                       rate = the radio HEARS the beacon but the demod garbles it (AGC/RF).
  * parse rejects    — frames with an out-of-range MPDU length (a descriptor/offset bug).
  * per-AP beacons   — clean vs CRC-error beacons for a target BSSID + the top talkers, so we
                       can see whether a specific AP's beacons arrive corrupt or not at all.

This reads at the chip's full delivery rate with trivial per-frame work, so it is the
"what can this card actually hear" ceiling — the live driver path (RxReaderThread → parser →
AP registry → UI) is what beacon_watch measures, and the gap between the two is host-side
RX-pipeline overhead, not the radio. No TX; read-only on the air.

    uv run python scripts/chips/rt5372/rx_diag.py --channel 1 --duration 20 [--bssid AA:BB:..]
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rt5372 import constants as C
from wifit3.chips.rt5372.driver import RT5372Driver
from wifit3.chips.rt5372.rx import _ieee80211_hdrlen, agc_to_rssi, probe_endpoints, read_rx_burst


def iter_all(buf, ev, lna):
    """Yield (frame_or_None, crc_error, rssi) for EVERY aggregated frame (CRC included)."""
    off = 0
    n = len(buf)
    while off + C.RXINFO_DESC_SIZE + C.RXWI_DESC_SIZE_4WORDS + C.RXD_DESC_SIZE <= n:
        rxinfo_w0 = struct.unpack_from("<I", buf, off)[0]
        rx_pkt_len = C.get_field(rxinfo_w0, C.RXINFO_W0_USB_DMA_RX_PKT_LEN)
        if rx_pkt_len == 0 or off + C.RXINFO_DESC_SIZE + rx_pkt_len + C.RXD_DESC_SIZE > n:
            break
        rxwi_off = off + C.RXINFO_DESC_SIZE
        rxwi_w0 = struct.unpack_from("<I", buf, rxwi_off)[0]
        rxwi_w2 = struct.unpack_from("<I", buf, rxwi_off + 8)[0]
        mpdu_len = C.get_field(rxwi_w0, C.RXWI_W0_MPDU_TOTAL_BYTE_COUNT)
        rxd_w0 = struct.unpack_from("<I", buf, off + C.RXINFO_DESC_SIZE + rx_pkt_len)[0]
        crc = bool(rxd_w0 & C.RXD_W0_CRC_ERROR)
        l2pad = bool(rxd_w0 & C.RXD_W0_L2PAD)
        fs = rxwi_off + C.RXWI_DESC_SIZE_4WORDS
        body = buf[fs:off + C.RXINFO_DESC_SIZE + rx_pkt_len]
        if l2pad and len(body) >= 2:
            hl = _ieee80211_hdrlen(body[0], body[1])
            if 0 < hl <= len(body) - 2:
                body = body[:hl] + body[hl + 2:]
        frame = bytes(body[:mpdu_len]) if 4 <= mpdu_len <= len(body) else None
        yield frame, crc, agc_to_rssi(rxwi_w2, ev, lna)
        off += (C.RXINFO_DESC_SIZE + rx_pkt_len + C.RXD_DESC_SIZE + 3) & ~3


def _bssid_of_beacon(frame: bytes) -> str | None:
    """addr3 (BSSID) of a beacon (FC type=mgmt subtype=beacon = 0x80)."""
    if len(frame) < 24 or frame[0] != 0x80:
        return None
    return ":".join(f"{b:02x}" for b in frame[16:22])


async def run(args) -> int:
    backend = libusb_package.get_libusb1_backend()
    entry = RT5372Driver.SUPPORTED_IDS[0]
    dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
    if dev is None:
        print(f"[FAIL] no {entry.vid:04x}:{entry.pid:04x} on the bus")
        return 1
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    driver = RT5372Driver.from_usb_device(dev, entry)
    loop = asyncio.get_running_loop()
    print("[*] cold bring-up...")
    await loop.run_in_executor(None, driver._bringup)
    eps = probe_endpoints(driver.transport.dev)
    bulk_in = eps.primary_bulk_in
    await loop.run_in_executor(None, driver._tune, args.channel)
    ev, lna = driver._eeprom, driver._lna_gain
    print(f"[*] ch{args.channel} bulk-IN 0x{bulk_in:02x} lna_gain={lna} "
          f"rssi_off={ev.rssi_offset_bg}; watching {args.duration:g}s...")

    target = args.bssid.lower() if args.bssid else None
    reads = timeouts = nbytes = maxbytes = 0
    frames = crc_frames = parse_rej = 0
    beac_ok = beac_crc = 0
    ap_ok: Counter = Counter()
    ap_crc: Counter = Counter()
    tgt_rssi: list[int] = []

    start = time.monotonic()
    while time.monotonic() - start < args.duration:
        buf = await loop.run_in_executor(None, read_rx_burst, dev, bulk_in)
        if buf is None:
            timeouts += 1
            continue
        reads += 1
        nbytes += len(buf)
        maxbytes = max(maxbytes, len(buf))
        for frame, crc, rssi in iter_all(buf, ev, lna):
            frames += 1
            if crc:
                crc_frames += 1
            if frame is None:
                parse_rej += 1
                continue
            bssid = _bssid_of_beacon(frame)
            if bssid is None:
                continue
            if crc:
                beac_crc += 1
                ap_crc[bssid] += 1
            else:
                beac_ok += 1
                ap_ok[bssid] += 1
            if target and bssid == target:
                tgt_rssi.append(rssi)

    await driver.close()
    dur = args.duration
    print("\n===== RX diagnostic =====")
    print(f"USB:    {reads} reads ({reads/dur:.0f}/s), {timeouts} timeouts, "
          f"{nbytes/1024:.0f} KB total, {nbytes//max(reads,1)} B/read avg, {maxbytes} B max "
          f"({'PINNED at 16K cap -> backpressure' if maxbytes >= 16384 else 'under cap'})")
    print(f"frames: {frames} total ({frames/dur:.0f}/s), {crc_frames} CRC-err "
          f"({100*crc_frames/max(frames,1):.0f}%), {parse_rej} parse-rejects")
    print(f"beacons: {beac_ok} clean ({beac_ok/dur:.1f}/s), {beac_crc} CRC-err "
          f"({100*beac_crc/max(beac_ok+beac_crc,1):.0f}% of beacons corrupt)")
    if target:
        n_ok, n_crc = ap_ok[target], ap_crc[target]
        rmin = min(tgt_rssi) if tgt_rssi else 0
        rmax = max(tgt_rssi) if tgt_rssi else 0
        print(f"target {target}: {n_ok} clean ({n_ok/dur:.1f}/s) + {n_crc} CRC-err "
              f"({n_ok+n_crc} heard = {(n_ok+n_crc)/dur:.1f}/s), RSSI {rmin}..{rmax} dBm")
    print("top APs (clean beacons/s):")
    for bssid, n in ap_ok.most_common(8):
        crc = ap_crc[bssid]
        print(f"  {bssid}  {n/dur:4.1f}/s clean  +{crc/dur:4.1f}/s crc  "
              f"({100*crc/max(n+crc,1):.0f}% corrupt)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RT5372 RX-drop diagnostic")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--bssid", default=None, help="target BSSID to break out (optional)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
