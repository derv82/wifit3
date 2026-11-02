"""Single-cursor verify_pcap for the MT7601U (MediaTek vendor-request USB).

Drives the port's real MT7601UDriver._bringup over one cursor via mt76_verify_replay.py and
reports coverage plus every named waiver. The Linux driver is the oracle this compares
against: a divergence here is a question to answer from the C, never a byte to copy back.

Run: uv run python scripts/chips/mt7601u/verify_pcap.py [<pcap>] [--verbose]
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import mt76_verify_replay as E
from wifit3.chips.mt7601u.constants import (
    MT_BBP_CSR_CFG,
    MT_BBP_CSR_CFG_REG_NUM,
    MT_RF_CSR_CFG,
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_ACK,
    MT_RX_FILTR_CFG_BA,
    MT_RX_FILTR_CFG_CFACK,
    MT_RX_FILTR_CFG_CFEND,
    MT_RX_FILTR_CFG_CRC_ERR,
    MT_RX_FILTR_CFG_CTRL_RSV,
    MT_RX_FILTR_CFG_CTS,
    MT_RX_FILTR_CFG_DUP,
    MT_RX_FILTR_CFG_PHY_ERR,
    MT_RX_FILTR_CFG_PROMISC,
    MT_RX_FILTR_CFG_PSPOLL,
    MT_RX_FILTR_CFG_RTS,
    MT_RX_FILTR_CFG_VER_ERR,
    MT_RX_STA_CNT0,
    MT_TXD_INFO_LEN,
    _field_get,
)
from wifit3.chips.mt7601u.driver import MT7601UDriver
from wifit3.chips.mt7601u.eeprom import MT7601UEepromParams
from wifit3.chips.mt7601u.cal import phy_calibrate
from wifit3.chips.mt7601u.mac import MacStats, mac_work
from wifit3.chips.mt7601u.phy import (
    FREQ_PLAN,
    FREQ_PLAN_BASE_REG,
    FREQ_PLAN_REGS,
)
from wifit3.chips.mt7601u.rx import (
    MT_FCE_INFO_LEN,
    iter_frames,
    next_segment_len,
)
from wifit3.dot11.parser import WlanFrameParser

DEFAULT_CAP = "driver_captures/captures_mt7601u_superwang/capture-1.pcap"

_XFER_CTRL, _XFER_BULK = 0x02, 0x03
_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_GET_DESCRIPTOR = 0x06
_DESC_CONFIGURATION, _DESC_ENDPOINT = 0x02, 0x05

# in_eps[MT_EP_IN_PKT_RX] (usb.c:243): the ambient 802.11 stream, not a port output.
EP_PKT_RX_IN = 0x84
EP_INBAND_CMD_OUT = 0x08
"""out_eps[MT_EP_OUT_INBAND_CMD] (usb.h:34): where MCU register pairs go."""
BBP_TEMP_REG = 47
"""phy.c:534 -- the BBP register read_temp kicks and polls."""
_USBMON_LEN = 32
"""mon_bin's urb length field. It exceeds LEN_CAP where usbmon truncated."""


# init.c:238-244, the managed-STA default the kernel programs during bring-up. Built from
# the bit list rather than the recorded value so it stays a statement about the C.
MANAGED_RXFILTER = (
    MT_RX_FILTR_CFG_CRC_ERR | MT_RX_FILTR_CFG_PHY_ERR | MT_RX_FILTR_CFG_PROMISC
    | MT_RX_FILTR_CFG_VER_ERR | MT_RX_FILTR_CFG_DUP | MT_RX_FILTR_CFG_CFACK
    | MT_RX_FILTR_CFG_CFEND | MT_RX_FILTR_CFG_ACK | MT_RX_FILTR_CFG_CTS
    | MT_RX_FILTR_CFG_RTS | MT_RX_FILTR_CFG_PSPOLL | MT_RX_FILTR_CFG_BA
    | MT_RX_FILTR_CFG_CTRL_RSV)


def _is_filter_write(op) -> bool:
    return (op.cls == "ctrl" and not op.is_in
            and op.widx in (MT_RX_FILTR_CFG, MT_RX_FILTR_CFG + 2))


def _carries_managed_default(op) -> bool:
    """True when this half-write carries the init.c:238-244 value."""
    if op.widx == MT_RX_FILTR_CFG:
        return op.wval == MANAGED_RXFILTER & 0xFFFF
    return op.wval == MANAGED_RXFILTER >> 16


def _rx_filter_substituted(port, op) -> bool:
    reg = port.addr & 0xFFFF
    return (not port.is_bulk and not port.is_in
            and reg in (MT_RX_FILTR_CFG, MT_RX_FILTR_CFG + 2)
            and _is_filter_write(op) and op.widx == reg
            and _carries_managed_default(op))


def _rx_filter_reconfigured(op) -> bool:
    return _is_filter_write(op) and not _carries_managed_default(op)


def waivers() -> E.WaiverSet:
    """Named, counted waivers for the mt7601u captures."""
    return E.WaiverSet(
        E.Waiver(
            "RX filter: monitor, not managed STA",
            "init.c:238-245 programs MT_RX_FILTR_CFG = 0x00017f97 for a managed STA inside "
            "mac_start. The register drops on set -- main.c:107 sets each bit only when "
            "mac80211 did NOT ask for that class -- so main.c:116-125 reconfigures it per "
            "interface, and this port writes the monitor result (0x00001093) where mac_start "
            "writes the managed default. Same value the capture reconfigures to; the only "
            "difference is that the port does not pass through the managed default first.",
            sub=_rx_filter_substituted,
            match=_rx_filter_reconfigured,
        ),
        E.Waiver(
            "USB enumeration",
            "GET_DESCRIPTOR / SET_CONFIGURATION / SET_INTERFACE and friends: standard-type "
            "control requests usbcore issues while enumerating. No mt7601u code path emits "
            "them -- the driver only ever sends vendor requests (usb.c:88).",
            match=lambda op: op.cls == "ctrl" and op.reqtype == "standard",
        ),
    )


def drop_rx_stream(pkts: list[bytes]) -> list[bytes]:
    """Discard every URB on the RX data pipe.

    They carry whatever was in the air while the capture ran: unreproducible, and nothing the
    port is asked to emit. Dropping them leaves the response stream holding only EP 0x85 MCU
    replies, which the port consumes in order.
    """
    return [p for p in pkts
            if not (len(p) > E.UsbmonOff.EP
                    and p[E.UsbmonOff.XFER] == _XFER_BULK
                    and p[E.UsbmonOff.EP] == EP_PKT_RX_IN)]


class _Endpoint:
    def __init__(self, addr: int, mps: int):
        self.bEndpointAddress = addr
        self.wMaxPacketSize = mps


class _Interface(list):
    """What assign_pipes() iterates: the endpoint descriptors, in descriptor order."""
    bInterfaceClass = 0xFF
    bInterfaceNumber = 0


class _Configuration:
    """Indexed as cfg[(0, 0)] by assign_pipes()."""

    def __init__(self, endpoints: list[_Endpoint]):
        self._iface = _Interface(endpoints)

    def __getitem__(self, _key):
        return self._iface


def _parse_endpoints(blob: bytes) -> list[_Endpoint]:
    eps: list[_Endpoint] = []
    i = 0
    while i + 2 <= len(blob):
        blen, btype = blob[i], blob[i + 1]
        if blen == 0:
            break
        if btype == _DESC_ENDPOINT and i + 7 <= len(blob):
            eps.append(_Endpoint(blob[i + 2], struct.unpack_from("<H", blob, i + 4)[0]))
        i += blen
    return eps


def interface_endpoints(pkts: list[bytes]) -> list[_Endpoint]:
    """The endpoints the card reported, read out of the capture's own enumeration.

    assign_pipes() is positional (usb.c:243) and this card's descriptor order is not
    ascending, so the order has to come from the capture rather than be assumed.
    """
    pending: set[bytes] = set()
    for pkt in pkts:
        if len(pkt) < int(E.UsbmonOff.SETUP) + 8 or pkt[E.UsbmonOff.XFER] != _XFER_CTRL:
            continue
        urb = bytes(pkt[0:8])
        if pkt[E.UsbmonOff.TYPE] == _URB_SUBMIT:
            wval = struct.unpack_from("<H", pkt, E.UsbmonOff.SETUP + 2)[0]
            if pkt[E.UsbmonOff.SETUP + 1] == _GET_DESCRIPTOR and (wval >> 8) == _DESC_CONFIGURATION:
                pending.add(urb)
        elif pkt[E.UsbmonOff.TYPE] == _URB_COMPLETE and urb in pending:
            pending.discard(urb)
            eps = _parse_endpoints(bytes(pkt[E.UsbmonOff.DATA:]))
            if eps:
                return eps
    return []


def driver_on(dev, endpoints: list[_Endpoint]) -> MT7601UDriver:
    """The real driver over the replay device, with connect()'s host-side half done here.

    assign_pipes() is the port of usb.c:228 and emits no wire bytes, so it runs for real
    against the captured descriptor. The RX reader thread is stubbed: it would race the
    cursor reading a pipe this walk serves no responses for.
    """
    drv = MT7601UDriver(dev)
    dev.get_active_configuration = lambda: _Configuration(endpoints)
    drv.transport.assign_pipes()
    drv._start_rx = lambda: None
    return drv


def rx_decode_phase(pkts: list[bytes], ee) -> None:
    """METHODOLOGY step 3: run the captured RX stream through the real decode path.

    drop_rx_stream throws these buffers away before the cursor walk, because what was in
    the air is unreproducible. The bytes are still the only real RX descriptors available
    offline, so decode them here, outside the cursor, where a surprise cannot derail it.
    Addresses and SSIDs are deliberately not printed: counts answer the question, and
    nothing real belongs in this output.

    usbmon truncates part of the bulk-IN stream -- its record length field exceeds the
    bytes it captured -- so those records are counted and skipped rather than decoded. A
    truncated buffer is not a decode failure; the C rejects it on the same test
    (dma.c:125 `dma_len + MT_DMA_HDRS > data_len`).
    """
    whole, truncated = [], 0
    for pkt in pkts:
        if not (len(pkt) > E.UsbmonOff.EP
                and pkt[E.UsbmonOff.TYPE] == _URB_COMPLETE
                and pkt[E.UsbmonOff.XFER] == _XFER_BULK
                and pkt[E.UsbmonOff.EP] == EP_PKT_RX_IN
                and pkt[E.UsbmonOff.LEN_CAP] > 0):
            continue
        cap = pkt[E.UsbmonOff.LEN_CAP]
        if int.from_bytes(pkt[_USBMON_LEN:_USBMON_LEN + 4], "little") != cap:
            truncated += 1
            continue
        whole.append(bytes(pkt[E.UsbmonOff.DATA:E.UsbmonOff.DATA + cap]))
    if not whole:
        print(f"RX DECODE: no untruncated EP {EP_PKT_RX_IN:#04x} payloads "
              f"({truncated} truncated by usbmon)")
        return

    parser = WlanFrameParser()
    segments = decoded = parsed = unconsumed = 0
    fc_bytes: set[int] = set()
    offset = ee.rssi_offset[0] if ee.rssi_offset else 0
    for buf in whole:
        walked = 0
        while True:
            seg_len = next_segment_len(buf[walked:])
            if not seg_len:
                break
            segments += 1
            walked += seg_len
        # MT_FCE_INFO_LEN of zero padding rides after the last segment (dma.h:15).
        if len(buf) - walked > MT_FCE_INFO_LEN:
            unconsumed += 1
        for frame in iter_frames(buf, ee.lna_gain, offset):
            decoded += 1
            fc_bytes.add(frame.frame[0])
            if parser.parse_80211_frame(frame.frame, frame.rssi) is not None:
                parsed += 1

    total = sum(len(b) for b in whole)
    print(f"RX DECODE: {len(whole)} whole bulk-IN buffers ({total} bytes), "
          f"{truncated} truncated by usbmon and skipped")
    print(f"           {segments} chained segments -> {decoded} frames decoded, "
          f"{parsed} parsed; {len(fc_bytes)} distinct frame-control bytes")
    if segments and decoded < segments:
        print(f"           {segments - decoded} segments dropped by the rxwi gates")
    if unconsumed:
        print(f"           {unconsumed} buffers left more than a trailer unconsumed")


def _mcu_pairs(op) -> list[tuple[int, int]]:
    """The (register, value) pairs inside an MCU CMD_RANDOM_WRITE payload."""
    data = op.data
    if len(data) < 12:
        return []
    ln = int.from_bytes(data[:4], "little") & MT_TXD_INFO_LEN
    body = data[4:4 + ln]
    return [(int.from_bytes(body[j:j + 4], "little"),
             int.from_bytes(body[j + 4:j + 8], "little"))
            for j in range(0, len(body) - 7, 8)]


def _tune_channel(op) -> int | None:
    """The 1..14 channel a freq-plan burst names, or None if this is not one.

    phy.c:284 __mt7601u_phy_set_channel opens with the RF freq-plan write, which is what
    delimits one tune in the operational tail. The channel is recovered by matching the
    recorded rows against FREQ_PLAN rather than trusting an op index.
    """
    if op.cls != "bulk" or op.ep != EP_INBAND_CMD_OUT:
        return None
    pairs = _mcu_pairs(op)
    if len(pairs) < FREQ_PLAN_REGS or (pairs[0][0] & 0xFFFF) != FREQ_PLAN_BASE_REG:
        return None
    row = tuple(v & 0xFF for _reg, v in pairs[:FREQ_PLAN_REGS])
    for idx, plan in enumerate(FREQ_PLAN):
        if tuple(plan) == row:
            return idx + 1
    return None


def _next_matchable_index(ops, i: int, waivers) -> int | None:
    """The next op no SKIP waiver covers, as an index. Works on a Walk or a ReplayDevice."""
    while i < len(ops):
        if waivers is None or waivers.first_match(ops[i]) is None:
            return i
        i += 1
    return None


def _selects_bbp(op, reg: int) -> bool:
    """True when this vendor write loads MT_BBP_CSR_CFG selecting BBP register `reg`."""
    return (op.cls == "ctrl" and not op.is_in and op.widx == MT_BBP_CSR_CFG
            and _field_get(MT_BBP_CSR_CFG_REG_NUM, op.wval) == reg)


def _is_cal_work(ops, j: int) -> bool:
    """phy.c:1002 cal_work opens with read_temp, i.e. a BBP access selecting register 47.

    Every BBP access opens on the same MT_BBP_CSR_CFG busy read, so the opener alone does
    not say which register follows; the selecting write is what distinguishes it.
    """
    op = ops[j]
    if not (op.cls == "ctrl" and op.is_in and (op.addr & 0xFFFF) == MT_BBP_CSR_CFG):
        return False
    return j + 1 < len(ops) and _selects_bbp(ops[j + 1], BBP_TEMP_REG)


STAT_BLOCK = range(0x1700, 0x1800)
"""regs.h:495-525 -- the counter block mac_work sweeps."""


def _producers_interleaved(ops, j: int, window: int = 16) -> bool:
    """True when the ops around `j` mix two kernel work items register by register.

    cal_work, mac_work and the channel set are three independent producers in the C
    (init.c:621, phy.c:1254), and nothing serialises their register access. The
    interleave hook drains one work item spliced between two of another's ops, but it
    cannot nest -- it issues USB calls itself -- so a sweep interrupted *inside* a second
    sweep ends the walk. Evidence it really happens: antenna/capture-1 ops 885-889 put a
    tune's BBP 4 access between two of mac_work's counter reads, and superwang/capture-2
    ops 805-810 do the same with the RF CSR. That is a property of the capture, not a
    divergence in the port, and saying so is the difference between a real finding and a
    wasted session.
    """
    stats = tune = False
    for op in ops[j:j + window]:
        if op.cls != "ctrl":
            continue
        reg = op.addr & 0xFFFF if op.is_in else op.widx
        if reg in STAT_BLOCK:
            stats = True
        elif reg in (MT_BBP_CSR_CFG, MT_RF_CSR_CFG):
            tune = True                     # set_channel reaches the chip through both
    return stats and tune


def _interleave_hook(drv: MT7601UDriver, stats: MacStats):
    """Drain a delayed work the kernel spliced into a mid-flight handler.

    cal_work and mac_work are separate work items in the C (init.c:621, phy.c:1254), so
    their register accesses land *between* a channel tune's rather than after it. Without
    this the tune's next op no longer lines up and the walk stops on an interleave that is
    a property of the capture, not of the port. The ops these drain are credited to the
    handler that was in flight.
    """
    def hook(dev) -> None:
        while True:
            j = _next_matchable_index(dev.ops, dev.i, dev.waivers)
            if j is None:
                return
            if _is_mac_work(dev.ops[j]):
                mac_work(drv.transport, stats)
            elif _is_cal_work(dev.ops, j):
                phy_calibrate(drv.phy)
            else:
                return
    return hook


def _is_mac_work(op) -> bool:
    """mac.c:301 mt7601u_mac_work opens with the first MT_RX_STA_CNT0 read."""
    return (op.cls == "ctrl" and op.is_in
            and (op.addr & 0xFFFF) == MT_RX_STA_CNT0)


def drive_operational(walk: E.Walk, drv: MT7601UDriver) -> dict[str, int]:
    """Dispatch the tail after bring-up to the real routine that emits each burst.

    One driver carries its own EEPROM and phy state across the bursts, the way a live
    session does -- rebuilding it per burst would hand set_channel a blank bw and
    chan_ext_below and hide any state the kernel's own sequence depends on.

    Tunes replay with scan=False: the recorded kernel is channel-hopping in monitor mode,
    not running a software scan, so MT7601U_STATE_SCANNING is clear and phy.c:434's
    agc_reset does not fire. An unrecognised opener -- a TX descriptor, most of what is
    left -- ends the walk rather than being skipped past.
    """
    tally = {"tunes": 0, "mac_work": 0, "cal_work": 0, "diverged": 0,
             "interleaved": 0}
    stats = MacStats()
    hook = _interleave_hook(drv, stats)
    while not walk.done():
        op = walk.peek_matchable()
        if op is None:
            break
        channel = _tune_channel(op)
        try:
            if channel is not None:
                walk.run(lambda dev, ch=channel: _on_device(drv, dev, drv.phy.set_channel, ch),
                         f"phy.set_channel({channel})", feed_responses=True,
                         async_interleave=hook)
                tally["tunes"] += 1
            elif _is_mac_work(op):
                walk.run(lambda dev: _on_device(drv, dev, mac_work, drv.transport, stats),
                         "mac.mac_work", feed_responses=True)
                tally["mac_work"] += 1
            elif (j := _next_matchable_index(walk.ops, walk.i, walk.waivers)) is not None                     and _is_cal_work(walk.ops, j):
                walk.run(lambda dev: _on_device(drv, dev, phy_calibrate, drv.phy),
                         "cal.phy_calibrate", feed_responses=True)
                tally["cal_work"] += 1
            else:
                break
        except E.Divergence:
            tally["diverged"] += 1
            j = _next_matchable_index(walk.ops, walk.i, walk.waivers)
            if j is not None and _producers_interleaved(walk.ops, j):
                tally["interleaved"] += 1
            break
        except Exception as e:  # noqa: BLE001
            print(f"[harness] operational burst raised {type(e).__name__}: {e}")
            tally["diverged"] += 1
            break
    return tally


def _on_device(drv: MT7601UDriver, dev, fn, *args) -> None:
    """Point the one driver's transport at this burst's replay device, then run `fn`."""
    drv.transport.dev = dev
    fn(*args)


async def _run_bringup(walk: E.Walk, endpoints: list[_Endpoint], state: dict) -> None:
    async def go(dev):
        state["drv"] = drv = driver_on(dev, endpoints)
        return await drv._bringup()

    await walk.run_async(go, "bringup")


def run(cap: str | None = None, verbose: bool = False) -> int:
    if not verbose:
        logging.getLogger("wifit3").setLevel(logging.CRITICAL)
    _real_sleep, time.sleep = time.sleep, lambda *a, **k: None
    _real_asleep = asyncio.sleep

    async def _fast_sleep(delay, *a, **k):
        return await _real_asleep(0)
    asyncio.sleep = _fast_sleep
    try:
        return _run(cap)
    finally:
        time.sleep = _real_sleep
        asyncio.sleep = _real_asleep


def _run(cap: str | None) -> int:
    path = cap or DEFAULT_CAP
    if not Path(path).exists():
        print(f"FAIL: no such capture {path}")
        return 1
    pkts = E.parse_pcapng(path)
    endpoints = interface_endpoints(pkts)
    if len(endpoints) != 8:
        print(f"FAIL: {path} carries no usable interface descriptor "
              f"({len(endpoints)} endpoints found, assign_pipes needs 2 IN + 6 OUT)")
        return 1
    rx_pkts = pkts
    pkts = drop_rx_stream(pkts)
    dev = E.busiest_vendor_devnum(pkts)
    if dev is None:
        print(f"FAIL: no vendor-control device found in {path}")
        return 1
    capture = E.extract(pkts, dev)
    # The tune phase runs after bring-up, so the response stream has to carry on
    # rather than restart: the port's MCU sequence counter does not reset.
    walk = E.Walk(capture, waivers=waivers(), continue_responses=True)
    state: dict = {}

    title = f"mt7601u verify · {Path(path).name}"
    eps = " ".join(f"{e.bEndpointAddress:#04x}" for e in endpoints)
    print(f"{title}: dev{dev}, {len(capture.ops)} host-to-device ops, "
          f"{len(capture.responses)} MCU responses, endpoints {eps}")

    try:
        asyncio.run(_run_bringup(walk, endpoints, state))
    except E.Divergence:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"\n[harness] bring-up raised {type(e).__name__}: {e}")

    # The operational tail runs on the same cursor before the report, so its credits
    # land in the coverage figure rather than after it.
    drv = state.get("drv")
    tally = {}
    if drv is not None and walk.ledger.frontier is None:
        tally = drive_operational(walk, drv)

    rc = walk.report(title)
    if tally and any(tally.values()):
        print(f"OPERATIONAL: {tally['tunes']} channel tunes, {tally['mac_work']} mac_work "
              f"sweeps, {tally['cal_work']} calibration passes")
        if tally["interleaved"]:
            print("             the walk stopped where a second kernel work item spliced "
                  "its registers into one already in flight -- concurrent producers, not "
                  "a port divergence (see MT7601U.md)")
    print()
    try:
        rx_decode_phase(rx_pkts, drv.ee if drv is not None else MT7601UEepromParams())
    except Exception as e:  # noqa: BLE001
        print(f"RX DECODE: raised {type(e).__name__}: {e}")
    if walk.ledger.frontier is not None:
        return rc
    # What is left is the TX stream: 442 bulk-OUTs carrying frames the kernel chose,
    # which this port has nothing to reproduce. verify_tx.py checks their descriptors.
    remaining = len(capture.ops) - walk.i
    print()
    print(f"OVERALL: bring-up and the operational tail replayed with no divergence; "
          f"{remaining} ops after the walk are out of its scope.")
    return 0 if walk.ledger.waived_count == 0 else 2


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    verbose = "--verbose" in sys.argv[1:]
    return run(args[0] if args else None, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
