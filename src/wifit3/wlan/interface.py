"""The device-agnostic radio (``WlanInterface``): per-card channel control, raw RX fan-out, and
frame injection via a chipset driver. The 802.11 picture (AP/client registry, WEP capture, packet
stats) lives in ``WlanSink``, owned by the ``WlanArray`` this interface is pooled into."""
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Callable, Any

import usb.core

from wifit3.chips.driver import FakeMacSupport
from wifit3.errors import (
    BringUpError, BringUpPermissionsError, is_device_gone, is_permission_error,
)
from wifit3.wlan.channels import scan_hop_order
from wifit3.dot11.packet import Packet
from wifit3.dot11.deauth import build_deauth, _deauth_nav_bytes

logger = logging.getLogger(__name__)


@dataclass
class DeauthResult:
    """Per-direction TX-ACK tally for a client-directed de-auth (see ``deauth_client``)."""
    client_acks: int = 0
    client_sent: int = 0
    ap_acks: int = 0
    ap_sent: int = 0
    measured: bool = False

    @property
    def total_acked(self) -> int:
        return self.client_acks + self.ap_acks

    @property
    def total_sent(self) -> int:
        return self.client_sent + self.ap_sent


class WlanInterface:
    """One card's radio: channel control, raw RX fan-out, and frame injection. Pooled into a
    ``WlanArray``, which owns the 802.11 picture and elects this card for attacks."""
    def __init__(self, driver_instance: Any, name: str, description: str,
                 vid: Optional[int] = None, pid: Optional[int] = None,
                 dev: Any = None, chipset: Optional[str] = None,
                 vendor: Optional[str] = None, product_name: Optional[str] = None):
        self.driver = driver_instance
        self.name = name
        self.description = description
        self.chipset = chipset
        self.vendor = vendor
        self.product_name = product_name
        self.vid = vid
        self.pid = pid
        self.dev = dev
        self.current_channel = 1

        self._rx_callbacks: List[Callable[[Packet], None]] = []
        self._disconnect_callbacks: List[Callable[[Exception], None]] = []
        self._device_lost = False

        # TX observer (frame_bytes) wired by WlanArray to WlanSink.record_tx: the array owns the
        # packet-stats picture, the radio just fires the event.
        self.on_tx: Optional[Callable[[bytes], None]] = None

        self._hopping_task: Optional[asyncio.Task] = None
        self._tune_task: Optional[asyncio.Task] = None
        self._is_hopping = False
        self._hop_lock = asyncio.Lock()

        self.driver.register_rx_callback(self._on_frame_parsed)
        self.driver.register_disconnect_callback(self._on_device_lost)

    def _on_frame_parsed(self, pkt: Packet) -> None:
        """Fan the driver's parsed frame out to raw subscribers (the array's _ingest, campaigns).
        The 802.11 picture is built from this stream by WlanArray/WlanSink, not here."""
        if self._rx_callbacks:
            self._fire_rx_callbacks(pkt)

    async def connect(self, progress_cb: Optional[Callable[[float, str], None]] = None) -> bool:
        """Initializes the underlying hardware handshake. A failure that is really the card being
        unopenable for a fixable access reason (Windows not WinUSB-bound, Linux EACCES/EBUSY) is
        re-raised as BringUpPermissionsError so the caller can offer the one-time setup."""
        try:
            return await self.driver.connect(progress_cb=progress_cb)
        except BringUpError as e:
            if is_permission_error(e):
                raise BringUpPermissionsError(e.stage, e.detail) from e
            raise
        except (usb.core.USBError, NotImplementedError) as e:
            if is_permission_error(e):
                raise BringUpPermissionsError("open", str(e)) from e
            raise

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel`` via the driver. ``scan=True`` (channel hopper) hints
        a transient hop so the driver may skip per-hop calibration."""
        success = await self.driver.set_channel(channel, scan=scan)
        if success:
            self.current_channel = channel
        return success

    async def set_fake_mac(self, mac: Any, bssid: Any = None) -> Optional[str]:
        """Ask the driver to HW-ACK frames addressed to ``mac`` (active-monitor). Returns the MAC
        the card will actually ACK as (the caller registers it as forged via the array), or None if
        the card can't spoof-ACK."""
        support = self.driver.FAKE_MAC
        if support in (FakeMacSupport.NONE, FakeMacSupport.UNIMPLEMENTED):
            logger.info("set_fake_mac: %s, active-monitor unavailable (%s)",
                        self._chipset, support.value)
            return None
        mac_b = self._to_mac_bytes(mac)
        bssid_b = self._to_mac_bytes(bssid) if bssid is not None else None
        assumed = await self.driver.enter_active_monitor(mac_b, bssid_b)
        assumed_str = ":".join(f"{b:02x}" for b in assumed)
        logger.info("[FAKEMAC] %s now HW-ACKing %s", self._chipset, assumed_str)
        return assumed_str

    async def clear_fake_mac(self) -> None:
        """Inverse of set_fake_mac: stop HW-ACKing the forged MAC."""
        if self.driver.FAKE_MAC in (FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC):
            await self.driver.exit_active_monitor()
            logger.info("[FAKEMAC] %s restored plain monitor", self._chipset)

    @staticmethod
    def _to_mac_bytes(mac: Any) -> bytes:
        """Coerce a MAC given as 6 raw bytes or a colon-separated string to bytes."""
        if isinstance(mac, bytes):
            return mac
        return bytes(int(x, 16) for x in str(mac).split(":"))

    @property
    def _chipset(self) -> str:
        """The chips/<name> dir of the active driver, for driver-specific log lines."""
        parts = type(self.driver).__module__.split(".")
        return parts[-2] if len(parts) >= 2 else parts[-1]

    def active_monitor_warning(self) -> Optional[str]:
        """Treelog warning (rich markup) if this card can't HW-ACK a spoofed MAC, else None."""
        support = self.driver.FAKE_MAC
        if support in (FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC):
            return None
        reason = "not possible (hard-MAC)" if support is FakeMacSupport.NONE else "not implemented"
        return (f"⚠  [orange1][bold]Active Monitor[/bold] {reason} "
                f"for [bold]{self._chipset}[/bold][/orange1]")

    def register_disconnect_callback(self, callback_func: Callable[[Exception], None]):
        """Register a subscriber for adapter loss: func(exc)."""
        if callback_func not in self._disconnect_callbacks:
            self._disconnect_callbacks.append(callback_func)

    def _on_device_lost(self, exc: Exception) -> None:
        """Single sink for 'adapter gone' from any source."""
        if self._device_lost:
            return
        self._device_lost = True
        self._is_hopping = False
        logger.error(f"[{self._chipset}] adapter lost: {exc}")
        for cb in list(self._disconnect_callbacks):
            try:
                cb(exc)
            except Exception:
                logger.exception("Disconnect callback failed")

    def register_rx_callback(self, callback_func: Callable[[Packet], None]):
        """Register a parsed-frame subscriber: func(pkt). This is the RAW per-card stream (the
        array's _ingest dedupes it; campaigns watch their own card's responses on it)."""
        if callback_func not in self._rx_callbacks:
            self._rx_callbacks.append(callback_func)

    def unregister_rx_callback(self, callback_func: Callable[[Packet], None]):
        """Idempotent inverse of register_rx_callback."""
        if callback_func in self._rx_callbacks:
            self._rx_callbacks.remove(callback_func)

    def _fire_rx_callbacks(self, pkt: Packet):
        for cb in self._rx_callbacks:
            try:
                cb(pkt)
            except Exception as e:
                logger.error(f"RX Callback failed: {e}")

    async def send_no_wait(self, frame_bytes: bytes) -> bool:
        """Inject a frame fire-and-forget."""
        if self.on_tx:
            self.on_tx(frame_bytes)          # array records TX packet-stats
        return await self.driver.inject_frame(frame_bytes)

    async def send_until_ack(self, frame_bytes: bytes, max_retries: int = 0) -> bool:
        """Inject a frame, then watch the monitor tap for the recipient's link-ACK, resending up
        to ``max_retries`` times on silence; returns whether it landed. Needs ``enable_rx_acks()``
        armed first, else fire-and-forget. Best-effort (see ``Driver.inject_frame_slow_retry``)."""
        if self.on_tx:
            self.on_tx(frame_bytes)
        return await self.driver.inject_frame_slow_retry(frame_bytes, max_resends=max_retries)

    async def enable_rx_acks(self) -> None:
        """Arm the driver's ACK tally so ``send_until_ack`` / ``acks_seen`` can observe the
        recipient's ACKs. A register write or a no-op, depending on the card."""
        await self.driver.enable_rx_acks()

    async def disable_rx_acks(self) -> None:
        """Disarm the ACK tally (inverse of ``enable_rx_acks``)."""
        await self.driver.disable_rx_acks()

    def acks_seen(self, mac: bytes) -> int:
        """ACKs the driver has tallied to source MAC ``mac`` since ``enable_rx_acks``."""
        return self.driver.acks_seen(mac)

    @property
    def supported_channels(self) -> List[int]:
        """Channels the active driver can tune to (delegates to the driver)."""
        return self.driver.SUPPORTED_CHANNELS

    def _deauth_frame(self, dest: bytes, src: bytes, ap_mac: bytes, dest_str: str) -> bytes:
        """One 802.11 deauth MPDU addressed dest←src, reason 7, destination-keyed ACK NAV."""
        return build_deauth(dest, src, ap_mac, 7, duration=_deauth_nav_bytes(dest_str))

    async def deauth_broadcast(self, ap_bssid: str, count: int = 20) -> int:
        """Spray AP→broadcast de-auth frames. The caller has this card tuned to the AP's channel."""
        ap_bssid = ap_bssid.lower()
        ap_mac = self._to_mac_bytes(ap_bssid)
        bcast = b"\xff\xff\xff\xff\xff\xff"
        frame = self._deauth_frame(bcast, ap_mac, ap_mac, "ff:ff:ff:ff:ff:ff")
        logger.info("Injecting broadcast de-auth (%dx) on CH %d from %s",
                    count, self.current_channel, ap_bssid)
        for _ in range(count):
            await self.send_no_wait(frame)
            await asyncio.sleep(0.01)
        return count

    async def deauth_client(self, ap_bssid: str, client_bssid: str,
                            rounds: int = 10) -> DeauthResult:
        """De-auth one client both ways, tallying how many frames each endpoint ACKed.

        AP->Client frames are ACKed by the CLIENT (the ACK's RA is the AP MAC we spoofed as
        sender); Client->AP frames are ACKed by the AP (RA = the client MAC we spoofed)."""
        ap_bssid = ap_bssid.lower()
        client_bssid = client_bssid.lower()
        ap_mac = self._to_mac_bytes(ap_bssid)
        cl_mac = self._to_mac_bytes(client_bssid)
        client_deauth = self._deauth_frame(cl_mac, ap_mac, ap_mac, client_bssid)   # AP→Client
        ap_deauth = self._deauth_frame(ap_mac, cl_mac, ap_mac, ap_bssid)           # Client→AP
        logger.info("Injecting client de-auth (%dx pairs) on CH %d: %s <-> %s",
                    rounds, self.current_channel, ap_bssid, client_bssid)

        driver = self.driver
        res = DeauthResult(measured=True)
        await driver.enable_rx_acks()
        try:
            for _ in range(rounds):
                landed_c = await self.send_until_ack(client_deauth)
                res.client_sent += 1
                if landed_c:
                    res.client_acks += 1
                landed_a = await self.send_until_ack(ap_deauth)
                res.ap_sent += 1
                if landed_a:
                    res.ap_acks += 1
                await asyncio.sleep(0.01)
        finally:
            await driver.disable_rx_acks()
        return res

    async def start_hopping(self, channels: List[int] = None, interval: float = 0.5):
        """Spawns an asyncio task to loop through channels."""
        async with self._hop_lock:
            if self._is_hopping:
                return

            if not channels:
                channels = self.supported_channels or [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]

            # Hop busy channels (1/6/11) first so the AP list fills before the scanner's
            # first sort tick. SUPPORTED_CHANNELS stays sequential for the filter UI.
            channels = scan_hop_order(channels)

            self._is_hopping = True
            self._hopping_task = asyncio.create_task(self._hop_loop(channels, interval))
            logger.info(
                "Started channel hopping on %s across %d channel(s) every %.2fs",
                self._chipset, len(channels), interval,
            )

    async def _hop_loop(self, channels: List[int], interval: float):
        import itertools
        channel_cycle = itertools.cycle(channels)
        last_channel = None
        while self._is_hopping:
            channel = next(channel_cycle)
            # Skip re-tuning the channel we're already on.
            if channel != last_channel:
                # Shield the tune
                self._tune_task = asyncio.ensure_future(
                    self.set_channel(channel, scan=True)
                )
                try:
                    await asyncio.shield(self._tune_task)
                except Exception as e:
                    if is_device_gone(e):
                        self._on_device_lost(e)
                    break
                last_channel = channel
            await asyncio.sleep(interval)

    async def stop_hopping(self):
        """Cancel the hopping task, then drain any in-flight tune."""
        async with self._hop_lock:
            task = self._hopping_task
            self._is_hopping = False
            self._hopping_task = None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            tune = self._tune_task
            self._tune_task = None
            if tune is not None and not tune.done():
                try:
                    await tune
                except Exception:
                    pass
            logger.info(f"Stopped channel hopping on {self._chipset}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()
