"""The card pool (``WlanArray``): one per session. It (1) consolidates every member card's raw RX
into a single deduplicated 802.11 picture (``WlanSink``), and (2) hands out a card for a campaign,
deauth or interaction via ``select_iface``. It is not a radio facade: it has no ``inject_frame`` or
``deauth``. Active TX runs on the selected ``WlanInterface`` itself; the array only picks it.

A single card is an array of one: dedupe is a no-op and every read passes straight through.
"""
import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Set

from wifit3.chips.driver import FakeMacSupport
from wifit3.dot11.packet import Packet
from wifit3.models import AccessPoint, Client
from wifit3.wlan.dedupe import StreamMerger
from wifit3.wlan.interface import WlanInterface
from wifit3.wlan.packet_stats import PacketStats
from wifit3.wlan.sink import WlanSink
from wifit3.wlan.wep_store import WepCaptureStore

logger = logging.getLogger(__name__)

# Prefer the most attack-capable card for TX (see _FAKE_MAC_RANK below).
_FAKE_MAC_RANK = {
    FakeMacSupport.SPOOFABLE: 0,
    FakeMacSupport.FIXED_MAC: 1,
    FakeMacSupport.NONE: 2,
    FakeMacSupport.UNIMPLEMENTED: 3,
}


class WlanArray:
    """A pool of WlanInterfaces feeding one shared WlanSink, plus card selection for attacks."""

    def __init__(self, sink: Optional[WlanSink] = None, window: float = 0.3):
        self._members: List[WlanInterface] = []
        self._sink = sink or WlanSink()
        self._dedupe = StreamMerger(window=window)
        self._rx_callbacks: List[Callable[[Packet], None]] = []      # deduped stream
        self._disconnect_callbacks: List[Callable[[Exception, int], None]] = []
        self._name_counter = 0
        # Hop state: the channel partition is computed only in start_hopping; every membership change
        # while hopping re-invokes it so the SPREAD tracks the live pool. Nothing outside drives it.
        self._hopping = False
        self._hop_channels: Optional[List[int]] = None
        self._hop_interval = 0.5
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rehop_tasks: Set[asyncio.Task] = set()

    # ----- pool membership ---------------------------------------------------

    @property
    def members(self) -> List[WlanInterface]:
        return list(self._members)

    def attach(self, iface: WlanInterface) -> WlanInterface:
        """Pool an already-connected interface: switch it to raw fan-out (the array's WlanSink is
        the picture now), point its TX stats at the sink, register it as a dedupe source, and
        subscribe the array to its raw RX + disconnect."""
        self._members.append(iface)
        self._dedupe.add_source(iface.name)
        iface.on_tx = self._sink.record_tx
        iface.register_rx_callback(lambda pkt, i=iface: self._ingest(i, pkt))
        iface.register_disconnect_callback(lambda exc, i=iface: self._member_lost(i, exc))
        logger.info("pool: attached %s (%s); %d card(s)", iface.name, iface.description,
                    len(self._members))
        if self._hopping:
            self._repartition()   # a card joined mid-hop: re-spread the channels across the pool
        return iface

    async def add(self, handle, *, connect: bool = True) -> WlanInterface:
        """Build a driver + interface from a discovery handle (dev, driver_cls, id_entry), connect
        it (the caller serializes calls: two cards driving RF bring-up over USB at once can collide),
        and pool it. Raises on bring-up failure; the caller decides splash-fatal vs toast."""
        dev, driver_cls, id_entry = handle
        driver = driver_cls.from_usb_device(dev, id_entry)
        name = f"wlan{self._name_counter}"
        self._name_counter += 1
        iface = WlanInterface(driver, name, id_entry.description,
                              vid=id_entry.vid, pid=id_entry.pid, dev=dev)
        if connect and not await iface.connect():
            raise RuntimeError(f"{id_entry.description}: connect returned False")
        return self.attach(iface)

    async def hotplug(self, handle, *, connect: bool = True) -> WlanInterface:
        """add() for a card that arrived mid-session (same build + connect + pool)."""
        return await self.add(handle, connect=connect)

    async def hot_unplug(self, iface: WlanInterface) -> None:
        """Deliberately drop a card: close it, remove it from the pool, re-emit disconnect."""
        try:
            await iface.close()
        except Exception:
            logger.exception("pool: close failed during hot_unplug of %s", iface.name)
        self._member_lost(iface, None)

    def _member_lost(self, iface: WlanInterface, exc: Optional[Exception]) -> None:
        """Drop a member (unplug or disconnect) and re-emit with the surviving card count so a
        caller can route: 0 -> back to splash, 1+ -> toast and keep running."""
        if iface in self._members:
            self._members.remove(iface)
        self._dedupe.remove_source(iface.name)
        remaining = len(self._members)
        logger.info("pool: lost %s; %d card(s) remain", iface.name, remaining)
        if self._hopping and remaining:
            self._repartition()   # survivors re-cover the departed card's channels
        for cb in list(self._disconnect_callbacks):
            try:
                cb(exc, remaining)
            except Exception:
                logger.exception("Array disconnect callback failed")

    # ----- card selection ----------------------------------------------------

    def select_iface(self, channel: int) -> Optional[WlanInterface]:
        """The most attack-capable card that can tune to ``channel``, or None when no live card can
        reach the band (e.g. a 5 GHz target with only 2.4 GHz cards left)."""
        cands = [m for m in self._members if channel in m.supported_channels]
        if not cands:
            return None
        return min(cands, key=lambda m: _FAKE_MAC_RANK.get(m.driver.FAKE_MAC, 9))

    # ----- deduped RX subscription (no v1 consumer, kept for future) ----------

    def register_rx_callback(self, cb: Callable[[Packet], None]) -> None:
        if cb not in self._rx_callbacks:
            self._rx_callbacks.append(cb)

    def unregister_rx_callback(self, cb: Callable[[Packet], None]) -> None:
        if cb in self._rx_callbacks:
            self._rx_callbacks.remove(cb)

    def register_disconnect_callback(self, cb: Callable[[Exception, int], None]) -> None:
        """Subscribe to member loss: cb(exc, remaining_card_count)."""
        if cb not in self._disconnect_callbacks:
            self._disconnect_callbacks.append(cb)

    def _ingest(self, iface: WlanInterface, pkt: Packet) -> None:
        """One card's raw frame: drop our own transmissions, dedupe across cards, and fold the novel
        copy into the picture (every card contributes its own signal, even on a duplicate)."""
        card_id = iface.name
        # Drop frames WE transmitted (forged attack MACs + our fake STA). Another pooled card hears
        # the TX card's injections over the air, and counting them inflates the RX picture: a WEP
        # ARP replay reuses one IV, so it is noise, not fresh keystream, yet it bloats the IV rate.
        if pkt.source in self._sink.forged_macs or pkt.source in self._sink.self_macs:
            return
        if self._dedupe.submit(card_id, pkt.raw, time.monotonic()):
            self._sink.update(pkt, card_id, channel_hint=iface.current_channel)
            for cb in self._rx_callbacks:
                try:
                    cb(pkt)
                except Exception:
                    logger.exception("Deduped RX callback failed")
        else:
            self._sink.record_signal(card_id, pkt.bssid, pkt.rssi)

    # ----- sink facade -------------------------------------------------------

    @property
    def access_points(self) -> Dict[str, AccessPoint]:
        return self._sink.access_points

    @property
    def clients(self) -> Dict[str, Client]:
        return self._sink.clients

    @property
    def forged_macs(self) -> Set[str]:
        return self._sink.forged_macs

    @property
    def wep_store(self) -> WepCaptureStore:
        return self._sink.wep_store

    @property
    def packet_stats(self) -> PacketStats:
        return self._sink.packet_stats

    def get_access_points(self) -> List[AccessPoint]:
        return self._sink.get_access_points()

    def register_forged_mac(self, mac) -> None:
        self._sink.register_forged_mac(mac)

    def register_self_mac(self, mac, bssid: Optional[str] = None) -> str:
        return self._sink.register_self_mac(mac, bssid)

    def unregister_self_mac(self, mac) -> None:
        self._sink.unregister_self_mac(mac)

    # ----- channel policy ----------------------------------------------------

    @property
    def supported_channels(self) -> List[int]:
        """Union of every member's channels (what the pool as a whole can tune to)."""
        seen: List[int] = []
        for m in self._members:
            for ch in m.supported_channels:
                if ch not in seen:
                    seen.append(ch)
        return sorted(seen)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """STACK: tune every channel-capable member to ``channel`` (focus/PBC). Members that do not
        support it, or are already on it, are left alone."""
        targets = [m for m in self._members
                   if channel in m.supported_channels and m.current_channel != channel]
        results = await asyncio.gather(*(m.set_channel(channel, scan=scan) for m in targets),
                                       return_exceptions=True)
        return any(r is True for r in results)

    def _partition(self, channels: List[int]) -> dict:
        """SPREAD: give each channel to one capable card, balancing counts, so N cards cover N-way
        more air per hop. Iterate channels high-first (5 GHz before 2.4 GHz) so a dual-band card
        absorbs the scarce 5 GHz work before the all-band 2.4 GHz channels spread — that keeps cards
        on-band and avoids costly band switches. Any card the spread leaves empty (more cards than
        channels) then hops every filter channel it supports, doubling up for redundant RX rather
        than stranding on its last channel. A channel no card supports is dropped."""
        assignment = {m: [] for m in self._members}
        for ch in sorted(channels, reverse=True):
            capable = [m for m in self._members if ch in m.supported_channels]
            if not capable:
                continue
            m = min(capable, key=lambda mm: len(assignment[mm]))
            assignment[m].append(ch)
        for m in assignment:
            if not assignment[m]:
                assignment[m] = [ch for ch in channels if ch in m.supported_channels]
        return {m: sorted(chs) for m, chs in assignment.items()}

    async def start_hopping(self, channels: Optional[List[int]] = None,
                            interval: float = 0.5) -> None:
        """SPREAD hop: partition the channel list across members; each hops only its subset. Records
        the config + running loop so a later membership change can re-partition on its own."""
        self._hopping = True
        self._hop_channels = channels
        self._hop_interval = interval
        self._loop = asyncio.get_running_loop()
        chans = channels or self.supported_channels
        assignment = self._partition(chans)
        await asyncio.gather(*(
            m.start_hopping(channels=subset, interval=interval)
            for m, subset in assignment.items() if subset
        ))

    async def stop_hopping(self) -> None:
        self._hopping = False
        for task in list(self._rehop_tasks):
            task.cancel()
        await asyncio.gather(*(m.stop_hopping() for m in self._members),
                             return_exceptions=True)

    def _repartition(self) -> None:
        """Re-run the hop partition for the current membership. Safe from any thread: attach() calls
        it on the event loop (create the task directly), while the RX-reader disconnect path calls it
        off the loop, so schedule onto the captured loop."""
        loop = self._loop
        if loop is None:
            return

        def _spawn() -> None:
            task = loop.create_task(self._rehop())
            self._rehop_tasks.add(task)
            task.add_done_callback(self._rehop_tasks.discard)

        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            _spawn()
        else:
            loop.call_soon_threadsafe(_spawn)

    async def _rehop(self) -> None:
        try:
            await self.start_hopping(self._hop_channels, self._hop_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("re-hop after membership change failed")

    async def close(self) -> None:
        await asyncio.gather(*(m.close() for m in self._members), return_exceptions=True)
        self._members.clear()
