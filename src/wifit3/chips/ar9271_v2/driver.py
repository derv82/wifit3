"""AR9271 (ath9k_htc) driver — clean-room v2 re-port from the v6.18.12 kernel source.

A fresh bring-up against the mainline ``htc_9271-1.4.0.fw`` protocol, verified op-by-op against
the cold-boot pcap (``scripts/ar9271_v2/verify_pcap.py``). The cold init + channel-hop sequencing
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
from typing import Callable, ClassVar, List, Optional

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rx_reader import RxReaderThread
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.wlan.packet import WlanFrameParser

from . import bringup, chan as chanmod, constants as C, firmware, reg as R, rx_decode, tx
from .transport import AR9271Transport

logger = logging.getLogger(__name__)

_RX_BUF_SIZE = 16384          # [SRC] hif_usb.h:60 MAX_RX_BUF_SIZE — one bulk-IN read


class AR9271V2Driver(Driver):
    """Atheros AR9271 / ALFA AWUS036NHA — 2.4 GHz, soft-MAC, ath9k_htc firmware."""

    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(C.AR9271_VID, C.AR9271_PID, "Atheros AR9271 (ALFA AWUS036NHA)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))   # 2.4 GHz only, no 5 GHz radio
    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = ["ath9k_htc"]   # modprobe blacklist hint
    LINUX_REPLUG_AFTER_MODPROBE: ClassVar[bool] = False   # self-colds: FW download re-enumerates
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device):
        self.transport = AR9271Transport(dev)
        self.is_warm = False
        self.mac_address: Optional[str] = None
        self.wmi = None                                  # WMI channel, set by cold_bringup
        self.hw = None                                   # AthHw, set by cold_bringup
        self.endpoints: dict = {}                        # HTC service -> endpoint id
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._init_ack_state()

    def _init_ack_state(self) -> None:
        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._all_acks_seen: int = 0
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK
        self._tx_frames: int = 0
        self._tx_unacked: int = 0

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "AR9271V2Driver":
        return cls(dev)

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
        self._init_ack_state()
        self._init_tx(endpoints)
        return self

    def _adopt(self, res: "bringup.BringupResult") -> None:
        """Take ownership of the state cold_bringup produced, and arm the TX path."""
        self.wmi = res.wmi
        self.hw = res.hw
        self.transport = res.wmi.t
        self.endpoints = res.endpoints
        self.mac_address = ":".join(f"{b:02x}" for b in res.hw.macaddr)
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

        # Warm card (firmware already running from a previous session)? Re-downloading firmware to a
        # running card re-enters live firmware and crashes it off the bus, and unlike the Realtek
        # family the AR9271 can't skip the download on warm — the firmware IS the protocol stack and
        # the one-shot HTC_READY won't fire again. So on warm we light-reattach: reuse the running
        # firmware + its deterministic HTC endpoint map, re-claim + clear the pipe halts, and
        # repopulate the host shadows read-only (bringup.warm_reattach). That's read-only and can't
        # bork the card; if it fails (WMI silent), fall back to a clean replug prompt.
        _p(0.02, "Probing card state...")
        if await loop.run_in_executor(None, self._is_chip_warm):
            self.is_warm = True
            _p(0.10, "Card warm — re-attaching to running firmware (no replug)...")
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
            except Exception as e:  # noqa: BLE001 — reattach is read-only; degrade to a replug ask
                logger.warning("ar9271_v2: warm reattach failed (%s) — falling back to replug", e)
                if self._reader is not None:
                    await self._reader.stop()
                    self._reader = None
                raise BringUpError(
                    "AR9271 is warm and couldn't be re-attached to. Please unplug the card, wait a "
                    "few seconds, replug, and try again.") from e

        cold_dev = self.transport.dev
        _p(0.10, "Downloading AR9271 firmware...")
        await loop.run_in_executor(None, firmware.download, self.transport, fw)
        try:
            usb.util.dispose_resources(cold_dev)        # release the cold handle as it reboots
        except Exception:
            pass

        _p(0.30, "Waiting for AR9271 to re-enumerate...")
        redev = await self._await_reenumeration()
        if redev is None:
            raise BringUpError(
                "re-enumeration",
                "card did not re-enumerate after firmware download — please replug and retry.",
            )
        self.transport = AR9271Transport(redev)

        _p(0.40, "Claiming USB interface...")
        await loop.run_in_executor(None, self._claim, redev)

        _p(0.45, "Starting RX reader + HTC/WMI init...")
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="ar9271v2-rx",
                                      on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()
        res = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
        self._adopt(res)
        _p(1.0, f"AR9271 monitor up (ch 1, {self.mac_address})")
        return True

    async def _await_reenumeration(self) -> Optional[usb.core.Device]:
        backend = libusb_package.get_libusb1_backend()
        for _ in range(12):                 # ~3 s; the chip boots its text image and re-attaches
            await asyncio.sleep(0.25)
            dev = usb.core.find(idVendor=C.AR9271_VID, idProduct=C.AR9271_PID, backend=backend)
            if dev is not None:
                return dev
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

    def _claim(self, dev: usb.core.Device) -> None:
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
        raise BringUpError(f"AR9271 interface not claimable after re-enumeration: {last}")

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
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames). RA=frame[4:10]
            # is the STA the AP ACKed; keep only ACKs to a MAC we inject as.
            if self._ack_detect_on and len(frame) == 10 and frame[0] == 0xD4:
                self._all_acks_seen += 1
                ra = frame[4:10]
                if ra in self._our_tx_macs:
                    self._ack_sightings[ra.hex()] = self._ack_sightings.get(ra.hex(), 0) + 1
                    self._ack_last_ts[ra] = time.monotonic()   # for inject wait-for-ack
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    # ---- TX (the UI/attacks await inject_frame; live firing is the user's gate) -------
    async def enable_ack_detect(self) -> None:
        """Arm the ACK tap. Pure software flag — the monitor RX filter already sets
        ATH9K_RX_FILTER_CONTROL (calcrxfilter, FIF_CONTROL), so ACK control frames are admitted;
        no register write needed. Not enter_active_monitor, which makes the chip emit ACKs."""
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._all_acks_seen = 0
        self._tx_frames = 0
        self._tx_unacked = 0
        self._ack_detect_on = True
        logger.info("AR9271 TX-ACK detection ON — observing our TX delivery")

    async def disable_ack_detect(self) -> None:
        """Disarm the ACK tap (software flag only; the RX filter is left at the monitor default)."""
        self._ack_detect_on = False

    def acks_seen(self, mac: bytes) -> int:
        """Count of ACKs observed addressed to ``mac`` (an injected source MAC) since enable."""
        return self._ack_sightings.get(bytes(mac).hex(), 0)

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """Transmit one 802.11 frame (deauth / PMKID auth+assoc / aireplay-ng ``--test``). The
        Driver contract is async + returns bool; the blocking bulk-OUT is offloaded so a TX
        burst doesn't stall the event loop (RX runs off-loop on its own thread). ``use_no_ack`` is
        accepted for the contract — AR9271 injected monitor frames already carry no QoS / no-ACK.

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and resends the
        same frame up to ``max_resends`` times if none comes, returning whether it landed; ``0`` =
        fire-and-forget (byte-identical to the prior behaviour)."""
        loop = asyncio.get_running_loop()
        ta = bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None   # AP ACKs back to TA
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None
        for _ in range(max_resends + 1):
            t0 = time.monotonic()
            cookie = await loop.run_in_executor(None, self._emit_frame, bytes(frame_bytes))
            # Free the TX slot now. The bitmap only exists to hold an in-flight skb until its
            # WMI_TXSTATUS arrives (kernel ath9k_htc_txstatus -> tx_clear_slot [SRC]
            # htc_drv_txrx.c:647); userland inject is fire-and-forget — no skb to track and nothing
            # consumes TXSTATUS at runtime, so the slot is done once bulk-OUT queues the frame.
            # Without this the 256-slot bitmap leaks one per frame and throws ENOBUFS after 256:
            # fatal to high-rate WEP replay/chopchop, invisible to low-volume deauth/PMKID/WPS.
            # (The verify gate drives _emit_frame + feeds recorded TXSTATUS directly — untouched.)
            self.tx_slots.clear(cookie)
            self._tx_frames += 1
            if not ack_gated:
                return True                 # fire-and-forget (deauth / WEP / current behaviour)
            if await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        self._tx_unacked += 1
        return False                        # never ACKed after every send

    async def _await_ack(self, ta: bytes, since: float, window: float) -> bool:
        """True if the tap observed an ACK to ``ta`` after ``since``, within ``window`` s.
        _dispatch runs on this loop, so a sleep yield lets a just-arrived ACK's timestamp land
        between checks."""
        deadline = since + window
        while time.monotonic() < deadline:
            if self._ack_last_ts.get(ta, 0.0) > since:
                return True
            await asyncio.sleep(0.001)
        return False

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
