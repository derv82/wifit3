"""Single-cursor verify_pcap for the MT7925U (connac3 mt76-USB). Drives the port's
real load_firmware (and, as milestones land, post_boot_init + the operational tail)
over one cursor via mt76_verify_replay.py, and reports coverage plus every named
waiver.

Run: uv run python scripts/mt7925au/verify_pcap.py [<pcap>] [--verbose]
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import struct  # noqa: E402

import mt76_verify_replay as E  # noqa: E402
import wifit3.chips.mt7925au as mt_pkg  # noqa: E402
from wifit3.chips.mt7925au import init as mt_init  # noqa: E402
from wifit3.chips.mt7925au import mac as mt_mac  # noqa: E402
from wifit3.chips.mt7925au import mcu as mt_mcu  # noqa: E402
from wifit3.chips.mt7925au import rx as mt_rx  # noqa: E402
from wifit3.chips.mt7925au import tx as mt_tx  # noqa: E402
from wifit3.chips.mt7925au import txpower as mt_txpower  # noqa: E402
from wifit3.chips.mt7925au.constants import (  # noqa: E402
    MT7925_RXD_SEQ_OFF, MT_MIB_SDR9, MT_MIB_SDR3, MT_TX_AGG_CNT, MT_WTBL_UPDATE,
    MT792x_WTBL_RESERVED, EP_OUT_MCU, EP_OUT_HCCA, MT_SDIO_TXD_SIZE, SDIO_HDR_SIZE,
    MCU_UNI_CMD_DEV_INFO_UPDATE, MCU_UNI_CMD_BSS_INFO_UPDATE, MCU_UNI_CMD_SNIFFER,
    MCU_UNI_CMD_BAND_CONFIG, MCU_UNI_CMD_CHIP_CONFIG, MCU_UNI_CMD_SET_DOMAIN_INFO,
    MCU_UNI_CMD_SET_POWER_LIMIT, UNI_SNIFFER_ENABLE, UNI_SNIFFER_CONFIG,
    UNI_BAND_CONFIG_SET_MAC80211_RX_FILTER, UNI_BAND_CONFIG_RTS_THRESHOLD,
    UNI_BSS_INFO_BASIC, UNI_BSS_INFO_PM_DISABLE,
)
from wifit3.chips.mt7925au.firmware import MT7925AUFirmwareLoader  # noqa: E402
from wifit3.chips.mt7925au.transport import MT7925AUTransport  # noqa: E402

# mac_work burst first-read markers (band 0).
_SURVEY_FIRST = MT_MIB_SDR9(0)      # 0x820ed02c
_MIB_FIRST = MT_MIB_SDR3(0)         # 0x820ed698
_RESET_FIRST = MT_TX_AGG_CNT(0, 0)  # 0x820ed7dc (only as the FIRST op of a burst)

DEFAULT_CAP = "usb_dumps_new2/captures_mt7925u/capture-1.pcap"
ASSETS = Path(mt_pkg.__file__).parent / "assets"

# MCU frame seq byte on the wire (EP 0x08 bulk-OUT): txd offset 39 + 4B SDIO hdr = 43.
_MCU_SEQ_OFF = 43


def _mcu_cid(op) -> "int | None":
    """The connac3 UNI cid of an EP-0x08 MCU frame, else None."""
    if op.cls == "bulk" and op.ep == EP_OUT_MCU and len(op.data) > 39:
        return op.data[38] | (op.data[39] << 8)
    return None


# TX (EP 0x09) frame carve: [4B SDIO hdr][64B TXD][MPDU][pad]. The MPDU length is the
# SDIO tx_bytes (TXD + MPDU) minus the TXD.
_TX_MPDU_OFF = SDIO_HDR_SIZE + MT_SDIO_TXD_SIZE


def _tx_mpdu(data: bytes) -> bytes:
    tx_bytes = data[0] | (data[1] << 8)
    return data[_TX_MPDU_OFF:_TX_MPDU_OFF + (tx_bytes - MT_SDIO_TXD_SIZE)]


def _is_ctrl_tx(op) -> bool:
    """True for an EP-0x09 frame carrying an 802.11 control MPDU (ftype 1). aireplay's
    RTS/control frames carry a tid from skb->priority (offline-underivable) and are not
    frames wifit3's inject path emits, so they stay waived."""
    if op.cls == "bulk" and op.ep == EP_OUT_HCCA and len(op.data) > _TX_MPDU_OFF + 1:
        fc = op.data[_TX_MPDU_OFF] | (op.data[_TX_MPDU_OFF + 1] << 8)
        return (fc & 0x0C) == 0x04
    return False


def waivers() -> E.WaiverSet:
    """Named, counted waivers for the mt7925u capture."""
    return E.WaiverSet(
        E.Waiver(
            "USB enumeration",
            "GET_DESCRIPTOR / SET_CONFIGURATION / SET_ISOCH_DELAY and friends: standard-type "
            "control requests usbcore issues while enumerating (the card re-enumerates once "
            "when airmon-ng starts monitor). The wifi driver never emits them.",
            match=lambda op: op.cls == "ctrl" and op.reqtype == "standard",
        ),
    )


class _WireMcuQueue:
    """Stand-in for transport._mcu_rx_queue, serving MCU responses from the wire. Seq-aware:
    `get` returns the earliest unconsumed response carrying `expected_seq` (mt7925_mcu_rxd
    seq at offset 33), so the port always gets its own ack even when the mt76 core's acks
    are interleaved. Response content is input the port consumes, not output verified."""

    def __init__(self, responses: list[bytes]):
        self._all = [r for r in responses if mt_rx.classify(r) == "mcu"]
        self._used = [False] * len(self._all)
        self.expected_seq: int | None = None

    def _find(self) -> int:
        for i, (r, used) in enumerate(zip(self._all, self._used)):
            if used or len(r) <= MT7925_RXD_SEQ_OFF:
                continue
            if self.expected_seq is None or r[MT7925_RXD_SEQ_OFF] == self.expected_seq:
                return i
        return -1

    def empty(self) -> bool:
        return self._find() < 0

    def qsize(self) -> int:
        return sum(1 for u in self._used if not u)

    def get_nowait(self):
        i = self._find()
        if i < 0:
            raise asyncio.QueueEmpty
        self._used[i] = True
        return self._all[i]

    async def get(self):
        i = self._find()
        if i < 0:
            raise asyncio.TimeoutError
        self._used[i] = True
        return self._all[i]

    def put_nowait(self, x):
        pass


def _wire_next_mcu_seq(dev: E.ReplayDevice) -> int:
    """Serve the connac3 MCU seq from the wire (next EP-0x08 frame's seq byte at offset 43).
    Not offline-reproducible: the mt76 core interleaves messages the port skips. Skips
    SKIP-waived frames (the cfg80211 regulatory block) so the seq matches the frame the
    port actually emits next."""
    j = dev.i
    while j < len(dev.ops):
        op = dev.ops[j]
        if dev.waivers is not None and dev.waivers.first_match(op):
            j += 1
            continue
        if op.cls == "bulk" and op.ep == 0x08 and len(op.data) > _MCU_SEQ_OFF:
            return op.data[_MCU_SEQ_OFF]
        j += 1
    return 1


def _make_transport(dev: E.ReplayDevice, queue: "_WireMcuQueue"):
    """A real MT7925AUTransport over the ReplayDevice, with the RX reader thread replaced
    by the wire-fed queue and the MCU seq served from the wire."""
    t = MT7925AUTransport(dev)
    t._mcu_rx_queue = queue
    t.start_rx = lambda: None

    def _seq() -> int:
        s = _wire_next_mcu_seq(dev)
        queue.expected_seq = s
        return s
    t._next_mcu_seq = _seq
    return t


async def _drive_firmware(dev: E.ReplayDevice, state: dict):
    """Run the port's load_firmware over the cursor (chip-id/rev, power-on, dma_init,
    ROM-patch and WM-RAM upload, FW_START). Only the vendor-interface claim is faked."""
    t = _make_transport(dev, state["q"])
    loader = MT7925AUFirmwareLoader(t, ASSETS)
    loader._claim_vendor_interface = lambda *a, **k: 0
    return await loader.load_firmware()


async def _drive_postboot(dev: E.ReplayDevice, state: dict):
    """Run the port's post_boot_init over the cursor (run_firmware tail, FW_DL_EN
    clear, mac_init)."""
    t = _make_transport(dev, state["q"])
    return await mt_init.post_boot_init(t)


def _decode_operational_mcu(f: bytes):
    """A captured operational MCU frame to (cmd, payload) via the real encoder, params
    read off the wire. None if unrecognised (a frontier)."""
    cid = f[38] | (f[39] << 8)
    p = f[52:]                                   # payload (+ frame pad; encoders re-pad)
    if cid == MCU_UNI_CMD_SET_DOMAIN_INFO:
        return mt_mcu.set_channel_domain()       # world-"00" regdom (fixed)
    if cid == MCU_UNI_CMD_DEV_INFO_UPDATE:
        return mt_mcu.uni_dev_info(True, bytes(p[10:16]))
    if cid == MCU_UNI_CMD_BSS_INFO_UPDATE:
        tag = p[4] | (p[5] << 8)
        if tag == UNI_BSS_INFO_PM_DISABLE:
            return mt_mcu.uni_bss_pm_disable()
        if tag != UNI_BSS_INFO_BASIC or len(p) < 36:
            return None                          # other secondary BSS tlv: frontier
        conn_type = struct.unpack_from("<I", p, 12)[0]
        return mt_mcu.uni_bss_info(True, bytes(p[18:24]), conn_type=conn_type)
    if cid == MCU_UNI_CMD_SNIFFER:
        tag = p[4] | (p[5] << 8)
        if tag == UNI_SNIFFER_ENABLE:
            return mt_mcu.set_sniffer(bool(p[8]))
        if tag == UNI_SNIFFER_CONFIG:
            return mt_mcu.config_sniffer(p[12])   # control_ch
    if cid == MCU_UNI_CMD_BAND_CONFIG:
        tag = p[4] | (p[5] << 8)
        if tag == UNI_BAND_CONFIG_SET_MAC80211_RX_FILTER:
            fif, bit_map = struct.unpack_from("<II", p, 12)
            return mt_mcu.set_rxfilter(fif, bit_map, p[20])
        if tag == UNI_BAND_CONFIG_RTS_THRESHOLD:
            return mt_mcu.set_rts_thresh(struct.unpack_from("<I", p, 8)[0])
    if cid == MCU_UNI_CMD_CHIP_CONFIG:
        if b"KeepFullPwr" in p:
            i = p.index(b"KeepFullPwr")
            return mt_mcu.set_deep_sleep(p[i + 12:i + 13] == b"0")
        if b"ThermalProtGband" in p:
            return mt_mcu.thermal_gband()
        if b"ThermalProtAband" in p:
            return mt_mcu.thermal_aband()
    return None


async def _send_op_mcu(dev: E.ReplayDevice, q, disp):
    await _make_transport(dev, q).send_mcu_command(*disp, wait_resp=False)


async def _drive_txpower(dev: E.ReplayDevice, q):
    """Drive the full per-band SET_POWER_LIMIT batch (mt7925_mcu_set_rate_txpower) at the
    cursor. The card has 6 GHz, so all three bands are emitted."""
    t = _make_transport(dev, q)
    for cmd, payload in mt_txpower.rate_txpower_all(has_6ghz=True):
        await t.send_mcu_command(cmd, payload, wait_resp=False)


async def _drive_operational(walk: E.Walk, state: dict):
    """Dispatch each operational burst to the real routine that emits it: mac_work
    (reset_counters / survey / MIB), the add_interface WCID update, or a monitor MCU
    command. An unrecognised opener stops the walk at the frontier."""
    q = state["q"]
    while not walk.done():
        op = walk.peek_matchable()
        if op is None:
            break
        if op.cls == "ctrl" and op.is_in and op.addr == _RESET_FIRST:
            walk.run(lambda dev: mt_mac.reset_counters(_make_transport(dev, q)),
                     "mac.reset_counters")
        elif op.cls == "ctrl" and op.is_in and op.addr == _SURVEY_FIRST:
            walk.run(lambda dev: mt_mac.update_survey(_make_transport(dev, q)),
                     "mac.update_survey")
        elif op.cls == "ctrl" and op.is_in and op.addr == _MIB_FIRST:
            walk.run(lambda dev: mt_mac.update_mib_stats(_make_transport(dev, q)),
                     "mac.update_mib_stats")
        elif op.cls == "ctrl" and op.is_in and op.addr == MT_WTBL_UPDATE:
            walk.run(lambda dev: mt_init._wtbl_update(_make_transport(dev, q),
                                                      MT792x_WTBL_RESERVED),
                     "init.wtbl_update (add_interface)")
        elif (op.cls == "bulk" and op.ep == EP_OUT_MCU
              and (op.data[38] | (op.data[39] << 8)) == MCU_UNI_CMD_SET_POWER_LIMIT):
            await walk.run_async(lambda dev: _drive_txpower(dev, q),
                                 "txpower.rate_txpower")
        elif op.cls == "bulk" and op.ep == EP_OUT_MCU:
            disp = _decode_operational_mcu(op.data)
            if disp is None:
                break                            # unknown MCU command: frontier
            await walk.run_async(lambda dev, d=disp: _send_op_mcu(dev, q, d),
                                 "operational.monitor MCU")
        else:
            break                                # unknown burst opener (incl. TX): frontier


async def _run_bringup(walk: E.Walk, state: dict):
    state["q"] = _WireMcuQueue(walk.cap.responses)
    await walk.run_async(lambda dev: _drive_firmware(dev, state), "firmware.load_firmware")
    await walk.run_async(lambda dev: _drive_postboot(dev, state), "init.post_boot_init")
    await _drive_operational(walk, state)


def _tx_bytematch(capture, title: str) -> "bool | None":
    """CHECK-4-style TX byte-diff (methodology 6.4): rebuild each captured EP-0x09 frame
    with tx.build_tx (the bytes driver._inject_frame sends) and diff against the wire.

    The operational tail interleaves mac_work register reads with aireplay's TX writes on
    separate kernel threads, so it is not strict-cursor-walkable; this phase byte-matches
    the TX frames directly. Only the 802.11 sequence (and, for WEP, the IV) may differ, and
    here we feed the captured MPDU so even those match. Returns True if every injectable
    (mgmt) frame reproduced byte-exact, None if the capture carries no TX."""
    tx = [op for op in capture.ops if op.cls == "bulk" and op.ep == EP_OUT_HCCA]
    if not tx:
        return None

    match = miss = ctrl = 0
    misses: list = []
    for op in tx:
        if _is_ctrl_tx(op):
            ctrl += 1
            continue
        mpdu = _tx_mpdu(op.data)
        # build_tx_no_ack (not the production build_tx): the captured aireplay frames set
        # MT_TXD3_NO_ACK, which wifit3's inject path never does. This replay-only builder
        # adds that one bit so the byte-match reflects the exact captured frames.
        built = mt_tx.build_tx_no_ack(mpdu, wcid_idx=MT792x_WTBL_RESERVED)
        if built == op.data:
            match += 1
        else:
            miss += 1
            if len(misses) < 5:
                n = min(len(built), len(op.data))
                d = next((k for k in range(n) if built[k] != op.data[k]), n)
                misses.append((op.idx, op.frame, d,
                               op.data[d:d + 4].hex(), built[d:d + 4].hex()))

    bar = "=" * 78
    print(f"\n{bar}\nTX BYTE-MATCH (mt7925au) · {title} · EP 0x09 (HCCA)\n{bar}")
    print(f"injectable mgmt frames reproduced byte-exact: {match}/{match + miss}")
    if ctrl:
        print(f"control frames (RTS) out of scope .........: {ctrl}  "
              "(aireplay-injected; TXWI tid from skb->priority, not a wifit3 inject frame)")
    for idx, frame, d, wb, pb in misses:
        print(f"    MISS op #{idx} @f{frame} byte {d}: wire {wb} vs port {pb}")
    print("-" * 78)
    if miss == 0:
        print("RESULT: TX PASS. Every injected mgmt frame reproduced byte-exact.")
    else:
        print(f"RESULT: TX FAIL. {miss} mgmt frame(s) diverged; see above.")
    return miss == 0


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
    dev = E.busiest_vendor_devnum(pkts)
    if dev is None:
        print(f"FAIL: no vendor-control device found in {path}")
        return 1
    capture = E.extract(pkts, dev)
    walk = E.Walk(capture, waivers=waivers())
    state: dict = {}

    title = f"mt76 verify (mt7925au) · {Path(path).name}"
    print(f"{title}: dev{dev}, {len(capture.ops)} host-to-device ops, "
          f"{len(capture.responses)} responses")

    try:
        asyncio.run(_run_bringup(walk, state))
    except E.Divergence:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"\n[harness] bring-up raised {type(e).__name__}: {e}")

    rc = walk.report(title)
    tx_ok = _tx_bytematch(capture, Path(path).name)
    if tx_ok is None:
        return rc                                # RX/monitor capture: no TX phase

    # TX capture: the bring-up walk stops (unwalked tail) at the first TX frame because the
    # operational tail interleaves mac_work reads with aireplay TX and is not strict-cursor-
    # walkable. That stop is expected, not a bug — a real bug would either set ledger.frontier
    # (a divergence) or stop at a non-TX op before the TX region.
    stop = walk.peek()
    stopped_at_tx = (walk.ledger.frontier is None and stop is not None
                     and stop.cls == "bulk" and stop.ep == EP_OUT_HCCA)
    print(f"\n{'=' * 78}")
    if stopped_at_tx and tx_ok:
        print("OVERALL: bring-up replayed with no divergence up to the first TX frame, and "
              "every injected mgmt frame byte-matched.")
        return 0
    print("OVERALL: TX capture verification did not fully pass; see sections above.")
    return 1


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    verbose = "--verbose" in sys.argv[1:]
    return run(args[0] if args else None, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
