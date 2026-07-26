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

import mt76_verify_replay as E  # noqa: E402
import wifit3.chips.mt7925au as mt_pkg  # noqa: E402
from wifit3.chips.mt7925au import init as mt_init  # noqa: E402
from wifit3.chips.mt7925au import rx as mt_rx  # noqa: E402
from wifit3.chips.mt7925au.constants import MT7925_RXD_SEQ_OFF  # noqa: E402
from wifit3.chips.mt7925au.firmware import MT7925AUFirmwareLoader  # noqa: E402
from wifit3.chips.mt7925au.transport import MT7925AUTransport  # noqa: E402

DEFAULT_CAP = "usb_dumps_new2/captures_mt7925u/capture-1.pcap"
ASSETS = Path(mt_pkg.__file__).parent / "assets"

# MCU frame seq byte on the wire (EP 0x08 bulk-OUT): txd offset 39 + 4B SDIO hdr = 43.
_MCU_SEQ_OFF = 43


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
    Not offline-reproducible: the mt76 core interleaves messages the port skips."""
    j = dev.i
    while j < len(dev.ops):
        op = dev.ops[j]
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
    clear, mac_init, CLC, monitor entry)."""
    t = _make_transport(dev, state["q"])
    return await mt_init.post_boot_init(t)


async def _run_bringup(walk: E.Walk, state: dict):
    state["q"] = _WireMcuQueue(walk.cap.responses)
    await walk.run_async(lambda dev: _drive_firmware(dev, state), "firmware.load_firmware")
    await walk.run_async(lambda dev: _drive_postboot(dev, state), "init.post_boot_init")


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

    return walk.report(title)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    verbose = "--verbose" in sys.argv[1:]
    return run(args[0] if args else None, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())
