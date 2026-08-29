"""AR9271 (ath9k_htc) driver — clean-room v2 re-port from the v6.18.12 kernel source.

A fresh bring-up against the mainline ``htc_9271-1.4.0.fw`` protocol, verified op-by-op against
the cold-boot pcap (``scripts/chips/ar9271_v2/verify_pcap.py``). The cold init + channel-hop sequencing
lives in ``bringup.py``; ``connect`` / ``set_channel`` drive it, and the same methods are what the
verify gate replays — so the bytes the gate checks are exactly the product's.

Live vs. gate: ``connect`` / ``set_channel`` detect a running asyncio loop. With one (the app),
they offload the blocking USB work to an executor and run the bulk-IN ``RxReaderThread``; with none
(the synchronous pcap gate over a ReplayDevice), they take the inline path. ``inject_frame`` stays
synchronous (the gate + unit tests drive it directly); the live-TX async wrapper lands with the
UI's TX wiring — live firing is the user's gate, not the agent's.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable, ClassVar, List, Optional, Tuple

import libusb_package
import usb.core
from usb.core import Device
import usb.util

from wifit3.chips.rx_reader import RxReaderThread
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.products import ALFA, TPLink
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from . import bringup, chan as chanmod, constants as C, firmware, htc, reg as R, rx_decode, tx
from .transport import AR9271Transport

logger = logging.getLogger(__name__)

_RX_BUF_SIZE = 16384          # [SRC] hif_usb.h:60 MAX_RX_BUF_SIZE — one bulk-IN read

# TP-Link OUIs (maclookup.app, 2026-08). 0cf3:9271 is only ever an AR9271, and TP-Link's sole AR9271
# product is the TL-WN722N v1, so any TP-Link OUI on this VID:PID identifies a v1 (see
# derive_product_name). Newer entries never match a v1 unit but are harmless; same product either way.
_TPLINK_OUIS = frozenset("""
00:0a:eb 00:14:78 00:19:e0 00:1d:0f 00:21:27 00:23:cd 00:25:86 00:27:19 04:f9:f8 08:1f:71
08:57:00 0c:4b:54 0c:72:2c 0c:80:63 0c:82:68 10:fe:ed 14:75:90 14:86:92 14:cc:20 14:cf:92
14:d8:64 14:e6:e4 18:a6:f7 18:d6:c7 18:f2:2c 1c:3b:f3 1c:44:19 1c:fa:68 20:6b:e7 20:dc:e6
24:5a:5f 24:69:68 28:2c:b2 28:ee:52 2c:79:be 30:b4:9e 30:b5:c2 30:fc:68 34:96:72 34:e8:94
34:f7:16 38:83:45 3c:06:a7 3c:46:d8 3c:6a:48 3c:84:6a 40:16:9f 40:3f:8c 44:66:90 44:b3:2d
48:0e:ec 48:5f:08 48:7d:2e 4c:10:d5 50:3e:aa 50:bd:5f 50:c7:bf 50:d4:f7 50:fa:84 54:75:95
54:a7:03 54:c8:0f 54:e6:fc 58:41:20 5c:63:bf 5c:89:9a 60:29:2b 60:32:b1 60:3a:7c 60:a3:e3
60:e3:27 64:56:01 64:66:b3 64:6e:97 64:70:02 68:77:24 68:dd:b7 68:ff:7b 6c:b1:58 6c:e8:73
70:4f:57 74:05:a5 74:39:89 74:da:88 74:ea:3a 78:44:fd 78:60:5b 78:a1:06 7c:8b:ca 7c:b5:9b
80:89:17 80:8f:1d 80:ae:54 80:ea:07 84:16:f9 84:b8:90 84:d8:1b 88:25:93 88:99:86 8c:21:0a
8c:a6:df 90:9a:4a 90:ae:1b 90:f6:52 94:0c:6d 94:d9:b3 98:48:27 98:97:cc 98:da:c4 98:de:d0
9c:21:6a 9c:47:82 9c:a6:15 a0:f3:c1 a4:1a:3a a4:2b:b0 a8:15:4d a8:57:4e ac:84:c6 b0:48:7a
b0:4e:26 b0:95:75 b0:95:8e b0:be:76 b8:f8:83 bc:46:99 bc:d1:77 c0:25:e9 c0:4a:00 c0:61:18
c0:c9:e3 c0:e4:2d c4:6e:1f c4:71:54 c4:e9:84 cc:08:fb cc:32:e5 cc:34:29 d0:37:45 d0:76:e7
d0:c7:c0 d4:01:6d d4:6e:0e d8:07:b6 d8:0d:17 d8:15:0d d8:47:32 d8:5d:4c dc:00:77 dc:fe:18
e0:05:c5 e4:c3:2a e4:d3:32 e8:94:f6 e8:de:27 ec:08:6b ec:17:2f ec:26:ca ec:60:73 ec:88:8f
f0:f3:36 f4:2a:7d f4:6d:2f f4:83:cd f4:84:8d f4:ec:38 f4:f2:6d f8:1a:67 f8:6f:b0 f8:8c:21
f8:c9:03 f8:ce:21 f8:d1:11 fc:d7:33
""".split())


class AR9271V2Driver(Driver):
    """Atheros AR9271 / ALFA AWUS036NHA — 2.4 GHz, soft-MAC, ath9k_htc firmware."""

    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))   # 2.4 GHz only, no 5 GHz radio
    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = ["ath9k_htc"]   # modprobe blacklist hint
    LINUX_REPLUG_AFTER_MODPROBE: ClassVar[bool] = True   # replug, not the fragile FW-download self-cold
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.transport = AR9271Transport(dev)
        self.is_warm = False
        self.mac_address: Optional[str] = None
        self.wmi = None                                  # WMI channel, set by cold_bringup
        self.hw = None                                   # AthHw, set by cold_bringup
        self.endpoints: dict = {}                        # HTC service -> endpoint id
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None

    @classmethod
    def from_usb_device(cls, dev: Device, id_entry: DeviceID) -> "AR9271V2Driver":
        drv = cls(dev)
        drv.product_name = id_entry.product_name   # SUPPORTED_IDS label; _adopt narrows by OUI
        return drv

    @staticmethod
    def derive_product_name(mac: Optional[str]) -> Optional[str]:
        """Split the shared 0cf3:9271 label by OUI: a TP-Link OUI is the TL-WN722N v1, any other
        real MAC is the ALFA AWUS036NHA. None only when the MAC is unknown (keeps the combined)."""
        if not mac:
            return None
        if mac[:8].lower() in _TPLINK_OUIS:
            return TPLink.TL_WN722N_V1
        return ALFA.AWUS036NHA

    def _refine_product_name(self) -> None:
        """Narrow the combined SUPPORTED_IDS label to one make once the EEPROM MAC is read; a
        MAC-less state leaves the combined label."""
        refined = self.derive_product_name(self.mac_address)
        if refined:
            self.product_name = refined

    @classmethod
    def for_replay(cls, wmi, hw, endpoints: dict) -> "AR9271V2Driver":
        """Build a driver around an already-brought-up ``(wmi, hw)`` for the TX unit tests
        (``tests/chips/ar9271_v2/test_tx.py``), which exercise ``inject_frame`` in isolation
        without a full ``connect``. The production + gate path is ``connect`` -> ``cold_bringup``,
        which adopts the same state via ``_adopt``."""
        self = cls.__new__(cls)
        self.wmi = wmi
        self.hw = hw
        self.transport = wmi.t
        self.endpoints = endpoints
        self._rx_callback = None
        self._reader = None
        Driver.__init__(self)       # base ACK tally (for_replay bypasses __init__)
        self._init_tx(endpoints)
        return self

    def _adopt(self, res: "bringup.BringupResult") -> None:
        """Take ownership of the state cold_bringup produced, and arm the TX path."""
        self.wmi = res.wmi
        self.hw = res.hw
        self.transport = res.wmi.t
        self.endpoints = res.endpoints
        self.mac_address = ":".join(f"{b:02x}" for b in res.hw.macaddr)
        self._refine_product_name()
        self._init_tx(res.endpoints)
        self._log_detected_config(res.hw)

    def _log_detected_config(self, hw) -> None:
        """One-line EEPROM-config summary: the board discriminators that pick the runtime-gated
        branches (tx-gain table, modal-header version, bb_desired_scale, in-band spur). Purely
        informational — no wire effect. The reference card reads: normal-power, modal v4,
        bb_scale 0, no in-band spur."""
        from .eeprom_4k import Map4k
        eep = Map4k(hw.eeprom)
        high = hw.eeprom[31] == R.AR5416_EEP_TXGAIN_HIGH_POWER
        bb_scale = eep.bb_scale_smrt_antenna & R.EEP_4K_BB_DESIRED_SCALE_MASK
        spur = eep.get_spur_channel(0) != R.AR_NO_SPUR
        logger.info(
            "ar9271_v2 EEPROM config: %s tx-gain, modal v%d, eep-rev 0x%x, bb_scale=%d, "
            "in-band-spur=%s, tx/rx-mask=%d/%d",
            "high-power" if high else "normal-power", eep.modal_version, eep.eeprom_rev,
            bb_scale, spur, hw.txchainmask, hw.rxchainmask)

    def _init_tx(self, endpoints: dict) -> None:
        """Resolve the TX service endpoints from the HTC handshake map and arm the slot bitmap.
        The endpoint ids are assigned by connect_service (don't hardcode 5/6) — mgmt frames ride
        WMI_MGMT_SVC, data frames the BE service (get_htc_epid's default AC) [SRC] htc_drv_txrx.c
        :102; injected monitor frames carry no QoS, so they all map to BE."""
        self.mgmt_epid = endpoints[C.WMI_MGMT_SVC]
        self.data_be_epid = endpoints[C.WMI_DATA_BE_SVC]
        self.tx_slots = tx.TxSlots()

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- bring-up ---------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Download firmware, then run the cold bring-up to a monitor receiver on ch1.

        With no running loop (the synchronous pcap gate), download + bring-up run inline over the
        ReplayDevice transport. Under the app's loop, the blocking USB work is offloaded to an
        executor and the bulk-IN ``RxReaderThread`` is started BEFORE the bring-up's RX-enable —
        the cold pipe wedges if the reader starts after RX is turned on [[rx_reader_thread]]."""
        def _p(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("ar9271_v2 %d%%: %s", int(pct * 100), msg)

        fw = firmware.load_firmware_blob()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Gate path: one transport throughout, no re-enumeration, no RX reader.
            firmware.download(self.transport, fw)
            self._adopt(bringup.cold_bringup(self.transport))
            return True

        try:
            return await self._bring_up_with_loop(loop, fw, _p)
        except htc.HTCReadyError as e:
            logger.warning("ar9271_v2: post-boot handshake mis-framed (first bytes %s); "
                           "tearing down for one clean retry", e.raw[:8].hex(" "))
            await self._teardown_cold_attempt()
            redev = await self._await_reenumeration(self.transport.dev)
            if redev is not None:
                self.transport = AR9271Transport(redev)
            try:
                return await self._bring_up_with_loop(loop, fw, _p)
            except Exception as e2:  # noqa: BLE001
                await self._teardown_cold_attempt()
                raise BringUpError(
                    "post-boot handshake",
                    "failed after firmware download; please replug and try again."
                ) from e2

    async def _bring_up_with_loop(self, loop, fw: bytes, _p) -> bool:
        """One full bring-up attempt under the app loop: warm-reattach if firmware is already
        running, else cold download + HTC/WMI init. Raises htc.HTCReadyError on a mis-framed READY."""
        _p(0.02, "Probing card state...")
        if await loop.run_in_executor(None, self._is_chip_warm):
            self.is_warm = True
            _p(0.10, "Card warm, re-attaching to running firmware (no replug)...")
            try:
                await loop.run_in_executor(None, self._claim, self.transport.dev)
                await loop.run_in_executor(None, self._clear_pipe_halts)
                self._reader = RxReaderThread(
                    loop, self._read_once, self._dispatch, name="ar9271v2-rx",
                    on_fatal=lambda e: self._on_lost and self._on_lost(e))
                self._reader.start()
                res = await loop.run_in_executor(None, bringup.warm_reattach, self.transport)
                self._adopt(res)
                _p(1.0, f"AR9271 re-attached warm (ch ?, {self.mac_address})")
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("ar9271_v2: warm reattach failed (%s); falling back to replug", e)
                if self._reader is not None:
                    await self._reader.stop()
                    self._reader = None
                raise BringUpError(
                    "warm reattach",
                    "failed on warm re-attach; please replug and try again."
                ) from e

        cold_dev = self.transport.dev
        cold_ports = self._get_port_numbers(cold_dev)
        unique_vidpid = False
        if not cold_ports:
            unique_vidpid = len(self._find_matching_ar9271_devices(cold_dev)) == 1

        _p(0.10, "Downloading AR9271 firmware...")
        try:
            await loop.run_in_executor(None, firmware.download, self.transport, fw)
        except usb.core.USBError as e:
            logger.warning("ar9271_v2: firmware download wedged (%s); falling back to replug", e)
            raise BringUpError(
                "firmware download",
                "USB timeout; please replug and try again."
            ) from e
        try:
            usb.util.dispose_resources(cold_dev)        # release the cold handle as it reboots
        except Exception:
            pass

        _p(0.30, "Waiting for AR9271 to re-enumerate...")
        redev = await self._await_reenumeration(cold_dev, unique_vidpid=unique_vidpid)
        if redev is None:
            raise BringUpError(
                "re-enumeration",
                "card did not re-enumerate after firmware download; please replug and retry.",
            )
        self.transport = AR9271Transport(redev)

        _p(0.40, "Claiming USB interface...")
        await loop.run_in_executor(None, self._claim, redev)

        _p(0.45, "Starting RX reader + HTC/WMI init...")
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="ar9271v2-rx",
                                      on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()
        try:
            res = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
        except usb.core.USBError as e:
            logger.warning("ar9271_v2: HTC/WMI init wedged (%s); falling back to replug", e)
            await self._teardown_cold_attempt()
            raise BringUpError(
                "HTC/WMI init",
                "USB timeout; please replug and try again."
            ) from e
        self._adopt(res)
        _p(1.0, f"AR9271 monitor up (ch 1, {self.mac_address})")
        return True

    async def _teardown_cold_attempt(self) -> None:
        if self._reader is not None:
            try:
                await self._reader.stop()
            except Exception:  # noqa: BLE001
                pass
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:  # noqa: BLE001
            pass

    def _get_port_numbers(self, dev: Device) -> Tuple[int, ...]:
        """Hub port numbers from root to ``dev``, via PyUSB's non-public ``port_numbers``."""
        return tuple(getattr(dev, "port_numbers", None) or ())

    def _find_matching_ar9271_devices(self, dev: Device) -> List[Device]:
        vid = getattr(dev, "idVendor", C.AR9271_VID)
        pid = getattr(dev, "idProduct", C.AR9271_PID)
        backend = libusb_package.get_libusb1_backend()
        found = usb.core.find(find_all=True, backend=backend,
                              idVendor=vid, idProduct=pid)
        return list(found or ())

    async def _await_reenumeration(self, original: Device, *, unique_vidpid: bool = False) -> Optional[Device]:
        """Reacquire this device, not an identical sibling.
        ``unique_vidpid`` is true if no other attached devices share this VID:PID."""
        og_bus = original.bus
        og_addr = original.address
        og_ports = self._get_port_numbers(original)
        for _ in range(12):                 # ~3 s; the chip boots its text image and re-attaches
            await asyncio.sleep(0.25)
            matching_devs = self._find_matching_ar9271_devices(original)
            for dev in matching_devs:
                if og_ports:
                    if dev.bus != og_bus or self._get_port_numbers(dev) != og_ports:
                        continue  # Plugged in to a different bus/port, ignore.
                elif (dev.bus, dev.address) != (og_bus, og_addr):
                    continue  # Not the same bus/address, ignore.
                return dev
            if not og_ports and unique_vidpid and len(matching_devs) == 1:
                return matching_devs[0]  # Single device for this VID:PID
        return None

    def _is_chip_warm(self) -> bool:
        """Detect a warm card (firmware already running) by smoke-testing the bulk-IN pipe: a
        firmware-running card in monitor mode streams HIF-framed RX (stream tag 0x4e00) on
        WLAN_RX 0x82, while a cold bootloader is silent there [[warm_reattach]]. Claim interface 0
        (the bulk pipe needs it), read a few times, and look for the tag. Any failure -> assume
        cold (the cold path is the safe default; it re-claims after re-enumeration)."""
        dev = self.transport.dev
        try:
            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass                                   # already configured
            usb.util.claim_interface(dev, 0)
        except (usb.core.USBError, NotImplementedError) as e:
            logger.debug("ar9271_v2: warm-probe claim failed (%s) -> assume cold", e)
            return False
        try:
            for _ in range(6):                         # warm returns on the first beacon (~ms)
                try:
                    buf = bytes(dev.read(C.EP_WLAN_RX, _RX_BUF_SIZE, 200))
                except usb.core.USBError:
                    continue                           # timeout / no traffic this read
                if len(buf) >= 4 and struct.unpack_from("<H", buf, 2)[0] == rx_decode.HIF_RX_STREAM_TAG:
                    return True
            return False
        finally:
            try:
                usb.util.release_interface(dev, 0)
            except Exception:
                pass

    def _clear_pipe_halts(self) -> None:
        """Reset the USB data-toggle bits on the four ath9k pipes. A warm card's pipes were
        mid-stream when the previous session detached, so host/device toggles can be desynced and
        the first transfers silently dropped; clear_halt resyncs them [v1 transport.reset_pipes]."""
        for ep in (C.EP_WLAN_TX, C.EP_WLAN_RX, C.EP_REG_IN, C.EP_REG_OUT):
            try:
                self.transport.dev.clear_halt(ep)
            except Exception as e:                 # noqa: BLE001 — best-effort toggle resync
                logger.debug("ar9271_v2: clear_halt(0x%02x) skipped: %s", ep, e)

    def _claim(self, dev: Device) -> None:
        """Configure + claim interface 0 on the (re-enumerated, firmware-booted) device. The
        bulk/interrupt pipes the bring-up + RX use need the interface claimed (EP0 control — the
        firmware download — does not, which is why that succeeds first). Right after re-enumeration
        Windows is still binding WinUSB, so the claim transiently fails with Access denied
        (errno 13) [SRC] the v1 driver's read-loop tolerated the same; retry through the settle,
        then the pipes are live."""
        last: Optional[Exception] = None
        for _ in range(40):                 # ~6 s of 0.15 s retries through the WinUSB re-bind
            try:
                try:
                    dev.set_configuration()
                except usb.core.USBError:
                    pass                    # already configured
                usb.util.claim_interface(dev, 0)
                return
            except (usb.core.USBError, NotImplementedError) as e:
                last = e
                time.sleep(0.15)
        raise BringUpError("claim", f"interface not claimable after re-enumeration: {last}")

    # ---- channel ----------------------------------------------------------
    async def set_channel(self, channel: int, scan: bool = False, *, _fastcc: bool = False) -> bool:
        """Tune to ``channel`` via a full ath9k_hw_reset (the always-correct retune). ``_fastcc``
        selects the kernel's within-band fast channel change — used only by the verify gate, which
        reads the full-vs-fast decision off the wire; the live hopper always takes the full reset
        (simple + safe). No running loop -> inline (the gate); otherwise offloaded."""
        ch = chanmod.channel_2ghz(channel)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._do_channel_change(ch, _fastcc)
            return True
        await loop.run_in_executor(None, self._do_channel_change, ch, _fastcc)
        return True

    def _do_channel_change(self, ch: chanmod.Channel, fastcc: bool) -> None:
        if fastcc:
            bringup.fast_channel_change(self.wmi, self.hw, ch)
        else:
            bringup.full_channel_change(self.wmi, self.hw, ch)

    # ---- RX (live only; the pcap gate does not model device->host frames) -
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read on WLAN_RX (None on a benign timeout).
        No traffic is a timeout, not an error — libusb raises USBTimeoutError (Windows WinUSB) or
        an ETIMEDOUT USBError (Linux); both mean "nothing to read", so swallow them (else the
        reader counts them as errors and gives up)."""
        try:
            return self.transport.wlan_in(_RX_BUF_SIZE)
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if e.errno == 110 or "tim" in str(e).lower():   # ETIMEDOUT / "timed out" / "timeout"
                return None
            raise

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the bulk-IN transfer into (mpdu, rssi) pairs (FCS stripped) and fan
        each parsed dict to the rx callback."""
        cb = self._rx_callback
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in rx_decode.iter_frames(buf):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
            # iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    # ---- RX-ACK detection (admit the recipient's ACK to RX) ---------------
    async def _enable_rx_acks(self) -> None:
        """No-op: the monitor RX filter already sets ATH9K_RX_FILTER_CONTROL (calcrxfilter runs
        with FIF_CONTROL, see bringup.cold_bringup), so the recipient's ACK control frames
        (FC=0xD4) already reach RX. Nothing to enable on the chip; the base arms the tally.
        Not enter_active_monitor, which makes the chip EMIT ACKs for a chosen MAC."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left at its default."""
        return

    # ---- TX (the UI/attacks await inject_frame; live firing is the user's gate) -------
    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the HTC TX wrapper for ``frame_bytes`` and bulk-OUT it once on the WLAN_TX pipe.
        The chip's own HW ACK-retry (AR_DRETRY_LIMIT STA short/long, programmed to the kernel
        INIT_SSH_RETRY/INIT_SLG_RETRY at MAC-queue init, not per-frame) is the only
        retransmission. The blocking bulk-OUT is offloaded so a TX burst doesn't stall the event
        loop; the pcap gate + unit tests drive ``_emit_frame`` directly (no running loop)."""
        loop = asyncio.get_running_loop()
        cookie = await loop.run_in_executor(None, self._emit_frame, bytes(frame_bytes))
        # Free the TX slot at emit time. The bitmap only holds an in-flight skb until its
        # WMI_TXSTATUS arrives (kernel ath9k_htc_txstatus -> tx_clear_slot [SRC] htc_drv_txrx.c:647);
        # userland inject is fire-and-forget — nothing consumes TXSTATUS at runtime, so the slot is
        # done once bulk-OUT queues the frame. Without this the 256-slot bitmap leaks one per frame
        # and throws ENOBUFS after 256: fatal to high-rate WEP replay/chopchop, invisible to
        # low-volume deauth/PMKID/WPS. (The verify gate drives _emit_frame directly — untouched.)
        self.tx_slots.clear(cookie)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Identity: the AR9271 does not software-stamp a sequence here (the injected frame carries
        the caller's own seqctl and ath9k leaves the sequence to hardware), so it goes out
        unchanged."""
        return frame_bytes

    def _emit_frame(self, dot11: bytes) -> int:
        """Sync core: allocate a TX slot, build the HTC wrapper (tx_frame_hdr for data, tx_mgmt_hdr
        otherwise; see ``tx.py``), and bulk-OUT it on the WLAN_TX pipe. Mirrors ath9k_htc_tx ->
        ath9k_htc_tx_start [SRC] htc_drv_main.c:862 / htc_drv_txrx.c:340. The 802.11 frame (incl. its
        sequence number) is the caller's; only the wrapper and the cookie are ours. Returns the
        allocated cookie (TX slot). The verify gate + unit tests drive this directly (no event loop);
        the public async ``inject_frame`` wraps it for the UI."""
        cookie = self.tx_slots.get()
        if tx.is_data_frame(dot11):
            frame = tx.build_data_tx(self.data_be_epid, dot11, cookie)
        else:
            frame = tx.build_mgmt_tx(self.mgmt_epid, dot11, cookie)
        self.wmi.t.wlan_out(frame)
        return cookie

    def tx_status_event(self, event_body: bytes) -> None:
        """ath9k_htc_txstatus [SRC] htc_drv_txrx.c:647 — a WMI_TXSTATUS event reports completed
        TX cookies; free each one's slot. In production this is dispatched from the WMI-event RX
        path; the verify gate feeds the recorded events, interleaved by capture order."""
        for cookie in tx.txstatus_cookies(event_body):
            self.tx_slots.clear(cookie)

    # ---- active monitor (HW-ACK a chosen MAC) — needed for ACKed conversations (WPS/EAP/PMKID) -
    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` into AR_STA_ID0/1 so the hardware HW-ACKs frames addressed to it (ath9k
        matches RA against AR_STA_ID) while staying in monitor mode — the prerequisite for any ACKed
        conversation (WPS/EAP/PMKID), where the AP retransmits and abandons the session if we don't
        ACK. Reversed by exit_active_monitor. ``bssid`` is unused (register-MAC ACK is a pure RA
        match). Mirrors the v1 driver + the Realtek siblings."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_sta_id, bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real EEPROM MAC in AR_STA_ID0/1 (stop ACKing the forged MAC)."""
        if self.hw is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_sta_id, bytes(self.hw.macaddr))

    def _write_sta_id(self, mac: bytes) -> None:
        """ath_hw_setbssidmask-style STA address write: low 4 bytes -> AR_STA_ID0, high 2 ->
        AR_STA_ID1 (preserving the upper opmode/KSRCH bits) [SRC] ath/hw.c ath_hw_setbssidmask."""
        self.hw.write(R.AR_STA_ID0, int.from_bytes(mac[0:4], "little"))
        id1 = (self.hw.read(R.AR_STA_ID1) & ~R.AR_STA_ID1_SADH_MASK) & 0xFFFFFFFF
        id1 |= int.from_bytes(mac[4:6], "little")
        self.hw.write(R.AR_STA_ID1, id1)

    async def close(self) -> None:
        if self._reader is not None:
            await self._reader.stop()        # join the reader BEFORE releasing the USB handle
            self._reader = None
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:
            pass
