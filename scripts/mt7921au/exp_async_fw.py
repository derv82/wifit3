"""
Does TRUE async (libusb_submit_transfer + a dedicated event thread) survive the
MT7921AU FW_START handoff?

Tonight's "deep pool" was sync blocking reads parked on threads — the URBs were
posted but completions waited on a blocked Python loop. This is the real thing:
async IN URBs on EP 0x84/0x85, each re-armed by its C callback the instant it
completes, with libusb's event loop running continuously on its own thread —
the kernel's I/O model. Only the IN side is async; the OUT/control path stays on
the production loader (which uploads cleanly on USB-2). Run on the USB-2 path.

SAFETY: this only reads/uploads-to-RAM — no EFUSE/flash writes, nothing that
survives a replug. The ctypes struct ABI is validated (sizeof + field offsets)
BEFORE any live USB call, so a layout mistake aborts loudly instead of crashing.

Usage: uv run python scripts/mt7921au/exp_async_fw.py [--pool 8] [--debug]
"""
import asyncio
import argparse
import ctypes
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

import wifit3.chips.mt7921au as mt_pkg
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader
from wifit3.chips.mt7921au.transport import MT7921AUTransport
# ruff: noqa: F403, F405
from wifit3.chips.mt7921au.constants import EP_IN_BULK, EP_IN_MCU

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")

# ---------------------------------------------------------------------------
# libusb 1.0 async ABI (mirrors libusb.h — cdecl on every platform, incl. Win)
# ---------------------------------------------------------------------------
LIBUSB_TRANSFER_TYPE_BULK = 2
_STATUS = {0: "COMPLETED", 1: "ERROR", 2: "TIMED_OUT", 3: "CANCELLED",
           4: "STALL", 5: "NO_DEVICE", 6: "OVERFLOW"}
LIBUSB_TRANSFER_NO_DEVICE = 5
LIBUSB_TRANSFER_CANCELLED = 3


class timeval(ctypes.Structure):
    # MSVC/glibc struct timeval: both members are `long` (4B on Win64 LLP64,
    # 8B on Linux LP64) — c_long matches the platform either way.
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class libusb_transfer(ctypes.Structure):
    pass


libusb_transfer_cb_fn = ctypes.CFUNCTYPE(None, ctypes.POINTER(libusb_transfer))
libusb_transfer._fields_ = [
    ("dev_handle", ctypes.c_void_p),
    ("flags", ctypes.c_uint8),
    ("endpoint", ctypes.c_ubyte),
    ("type", ctypes.c_ubyte),
    ("timeout", ctypes.c_uint),
    ("status", ctypes.c_int),
    ("length", ctypes.c_int),
    ("actual_length", ctypes.c_int),
    ("callback", libusb_transfer_cb_fn),
    ("user_data", ctypes.c_void_p),
    ("buffer", ctypes.c_void_p),
    ("num_iso_packets", ctypes.c_int),
]


def validate_abi() -> bool:
    """Fail loudly before any live USB call if the struct layout is wrong."""
    psize = ctypes.sizeof(ctypes.c_void_p)
    off = {f[0]: getattr(libusb_transfer, f[0]).offset for f in libusb_transfer._fields_}
    logger.info(f"ABI: pointer={psize}B sizeof(libusb_transfer)={ctypes.sizeof(libusb_transfer)} "
                f"callback@{off['callback']} buffer@{off['buffer']} num_iso@{off['num_iso_packets']}")
    if psize == 8:  # 64-bit reference values from libusb.h
        exp = {"dev_handle": 0, "callback": 32, "user_data": 40, "buffer": 48, "num_iso_packets": 56}
        if ctypes.sizeof(libusb_transfer) != 64 or any(off[k] != v for k, v in exp.items()):
            logger.error(f"ABI MISMATCH (64-bit): offsets={off} — aborting before USB.")
            return False
    return True


def load_async_lib():
    """Own CDLL handle to the same bundled libusb (never mutate pyusb's lib).
    Set explicit argtypes/restype so 64-bit pointers aren't truncated."""
    backend = libusb_package.get_libusb1_backend()
    lib = ctypes.CDLL(backend.lib._name)
    P = ctypes.POINTER(libusb_transfer)
    lib.libusb_alloc_transfer.argtypes = [ctypes.c_int]
    lib.libusb_alloc_transfer.restype = P
    lib.libusb_submit_transfer.argtypes = [P]
    lib.libusb_submit_transfer.restype = ctypes.c_int
    lib.libusb_cancel_transfer.argtypes = [P]
    lib.libusb_cancel_transfer.restype = ctypes.c_int
    lib.libusb_free_transfer.argtypes = [P]
    lib.libusb_free_transfer.restype = None
    lib.libusb_handle_events_timeout.argtypes = [ctypes.c_void_p, ctypes.POINTER(timeval)]
    lib.libusb_handle_events_timeout.restype = ctypes.c_int
    return backend, lib


def get_handles(dev, backend):
    """Pull pyusb's already-open libusb_device_handle* + libusb_context*."""
    dev._ctx.managed_open()
    dh = dev._ctx.handle
    raw = getattr(dh, "handle", dh)
    handle_value = raw.value if isinstance(raw, ctypes.c_void_p) else int(raw)
    ctx = backend.ctx  # c_void_p
    logger.info(f"handles: dev_handle=0x{handle_value:x} ctx={ctx}")
    if not handle_value:
        raise RuntimeError("could not extract libusb device handle from pyusb")
    return ctx, handle_value


# ---------------------------------------------------------------------------
# Transport whose IN drainer is a true-async URB pool
# ---------------------------------------------------------------------------
class AsyncInTransport(MT7921AUTransport):
    def __init__(self, dev, lib, ctx, handle_value, pool=8, buf=2048):
        super().__init__(dev)
        self._lib = lib
        self._ctx = ctx
        self._handle_value = handle_value
        self._pool_n = pool
        self._buf_sz = buf
        self._transfers = []          # POINTER(libusb_transfer), kept alive
        self._buffers = []            # ctypes buffers, kept alive
        self._cb = None               # CFUNCTYPE wrapper, kept alive
        self._event_thread = None
        self._stop = threading.Event()           # stops the event loop
        self._stop_resubmit = threading.Event()  # callbacks stop re-arming
        self._final_count = 0         # transfers whose terminal callback has fired
        self._completions = 0

    async def start_mcu_drainer(self, pool=None):
        if self._mcu_drainer_running:
            return
        self._mcu_drainer_running = True
        self._cb = libusb_transfer_cb_fn(self._on_complete)
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True, name="libusb-ev")
        self._event_thread.start()
        for ep in (EP_IN_BULK, EP_IN_MCU):
            for _ in range(self._pool_n):
                self._alloc_submit(ep)
        logger.info(f"async IN pool: {self._pool_n} URBs/EP on 0x{EP_IN_BULK:02x}+0x{EP_IN_MCU:02x}, "
                    f"event thread running")

    def _alloc_submit(self, ep):
        tptr = self._lib.libusb_alloc_transfer(0)
        if not tptr:
            logger.error("libusb_alloc_transfer returned NULL")
            return
        buf = (ctypes.c_ubyte * self._buf_sz)()
        t = tptr.contents
        t.dev_handle = self._handle_value
        t.endpoint = ep
        t.type = LIBUSB_TRANSFER_TYPE_BULK
        t.timeout = 0                 # infinite; we cancel on shutdown
        t.buffer = ctypes.cast(buf, ctypes.c_void_p)
        t.length = self._buf_sz
        t.callback = self._cb
        t.user_data = None
        self._transfers.append(tptr)
        self._buffers.append(buf)
        r = self._lib.libusb_submit_transfer(tptr)
        if r != 0:
            logger.error(f"libusb_submit_transfer(0x{ep:02x}) -> {r}")

    def _on_complete(self, tptr):
        # Runs on the libusb event thread.
        t = tptr.contents
        status = t.status
        if status == 0 and t.actual_length > 0:
            data = ctypes.string_at(t.buffer, t.actual_length)
            self._completions += 1
            try:
                self._loop.call_soon_threadsafe(self._safe_put, data)
            except RuntimeError:
                pass  # loop closed during shutdown
        # Re-arm unless we're tearing down or the device is gone. A transfer that
        # is NOT re-armed has fired its terminal callback — count it so teardown
        # only frees transfers libusb is finished with (free-while-pending crashes).
        if self._stop_resubmit.is_set() or status == LIBUSB_TRANSFER_NO_DEVICE:
            self._final_count += 1
        else:
            self._lib.libusb_submit_transfer(tptr)

    def _safe_put(self, data):
        try:
            self._mcu_rx_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _event_loop(self):
        tv = timeval(0, 20000)  # 20 ms
        while not self._stop.is_set():
            self._lib.libusb_handle_events_timeout(self._ctx, ctypes.byref(tv))

    async def stop_mcu_drainer(self):
        if not self._mcu_drainer_running:
            return
        self._mcu_drainer_running = False
        # libusb contract: a transfer may only be freed (and the handle closed)
        # after its callback has fired. So: stop re-arming, cancel everything,
        # keep the event thread running until every transfer's terminal callback
        # has landed, THEN stop the thread and free.
        self._stop_resubmit.set()
        for tptr in self._transfers:
            self._lib.libusb_cancel_transfer(tptr)
        for _ in range(150):          # up to ~3s for all cancellations to land
            if self._final_count >= len(self._transfers):
                break
            await asyncio.sleep(0.02)
        self._stop.set()
        if self._event_thread:
            self._event_thread.join(timeout=1.0)
        if self._final_count >= len(self._transfers):
            for tptr in self._transfers:
                self._lib.libusb_free_transfer(tptr)
        else:
            logger.warning(f"{len(self._transfers) - self._final_count} transfer(s) still "
                           f"pending — leaking rather than freeing (process is exiting)")
        logger.info(f"async drainer stopped (IN completions with data: {self._completions})")
        self._transfers = []
        self._buffers = []
        while not self._mcu_rx_queue.empty():
            try:
                self._mcu_rx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


def claim_vendor_iface(dev):
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == 0xFF:
            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
            except Exception as e:
                logger.debug(f"claim: {e}")
            return intf.bInterfaceNumber


async def main(pool, debug):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if not validate_abi():
        return 2

    backend, lib = load_async_lib()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)
    ctx, handle_value = get_handles(dev, backend)

    transport = AsyncInTransport(dev, lib, ctx, handle_value, pool=pool)
    loader = MT7921AUFirmwareLoader(transport, Path(mt_pkg.__file__).parent / "assets")

    logger.info("=== Experiment: true-async IN URBs across FW_START ===")
    t0 = time.monotonic()
    try:
        ok = await loader.load_firmware()
    finally:
        # load_firmware's finally already stopped the drainer (transfers cancelled
        # + freed); close the handle here, controlled, instead of at atexit.
        usb.util.dispose_resources(dev)
    logger.info(f"=== load_firmware() returned {ok} in {time.monotonic()-t0:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. TRUE-ASYNC SURVIVES THE FW_START HANDOFF.")
        return 0
    logger.info("RESULT: still no boot — async I/O did not survive FW_START either.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.pool, args.debug)))
