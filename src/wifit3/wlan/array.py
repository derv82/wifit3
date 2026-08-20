"""The card pool (``WlanArray``): one per session. It (1) consolidates every member card's raw RX
into a single deduplicated 802.11 state (``WlanSink``), and (2) hands out a card for a campaign,
deauth or interaction via ``select_iface``. It is not a radio facade: it has no ``inject_frame`` or
``deauth``. Active TX runs on the selected ``WlanInterface`` itself; the array only picks it.

A single card is an array of one: dedupe is a no-op and every read passes straight through.
"""
import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Set

from wifit3.chips.driver import FakeMacSupport
from wifit3.dot11.packet import BeaconPacket, Packet
from wifit3.models import AccessPoint, Client
from wifit3.wlan.dedupe import StreamMerger
from wifit3.wlan.interface import WlanInterface
from wifit3.wlan.packet_stats import PacketStats
from wifit3.wlan.sink import WlanSink
from wifit3.wlan.lease import Lease
from wifit3.wlan.wep_store import WepCaptureStore

logger = logging.getLogger(__name__)

# Prefer the most attack-capable card for TX (see fake_mac_rank below).
_FAKE_MAC_RANK = {
    FakeMacSupport.SPOOFABLE: 0,
    FakeMacSupport.FIXED_MAC: 1,
    FakeMacSupport.NONE: 2,
    FakeMacSupport.UNIMPLEMENTED: 3,
}


def fake_mac_rank(iface) -> int:
    """A card's TX-capability rank, lower = more attack-capable (UNIMPLEMENTED/unknown sorts last).
    The public read of _FAKE_MAC_RANK for callers that rank cards (select_iface, the TX picker)."""
    return _FAKE_MAC_RANK.get(getattr(getattr(iface, "driver", None), "FAKE_MAC", None), 9)


class WlanArray:
    """A pool of WlanInterfaces feeding one shared WlanSink, plus card selection for attacks."""

    def __init__(self, sink: Optional[WlanSink] = None, window: float = 0.3):
        self._members: List[WlanInterface] = []
        self._preferred: Optional[WlanInterface] = None   # user's session TX pick; see select_iface
        self._sink = sink or WlanSink()
        self._dedupe = StreamMerger(window=window)
        self._stray_beacon_channels: Dict[str, int] = {}  # bssid -> decoy channel; its beacons are ours
        self._evil_twin_bssids: Set[str] = set()          # our own twin APs; hidden from the scanner
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
        self._close_tasks: Set[asyncio.Task] = set()   # closing vanished cards; never cancelled

    # ----- membership --------------------------------------------------------

    @property
    def members(self) -> List[WlanInterface]:
        return list(self._members)

    def contains(self, device_id) -> bool:
        """Whether the exact card (bus, address) named by ``device_id`` is already attached."""
        return any(m.instance_key == device_id.instance_key for m in self._members)

    def attach(self, iface: WlanInterface) -> WlanInterface:
        """Attach an already-connected interface."""
        self._members.append(iface)
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()   # for scheduling re-hop / close from off-loop later
            except RuntimeError:
                pass
        self._dedupe.add_source(iface.name)
        iface.on_tx = self._sink.record_tx
        iface.register_rx_callback(lambda pkt, i=iface: self._ingest(i, pkt))
        iface.register_disconnect_callback(lambda exc, i=iface: self._member_lost(i, exc))
        logger.info("attached %s (%s); %d card(s)", iface.name, iface.description,
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
                              vid=id_entry.vid, pid=id_entry.pid, dev=dev,
                              chipset=id_entry.chipset, vendor=id_entry.vendor,
                              product_name=id_entry.product_name,
                              bus=dev.bus, address=dev.address)
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
            logger.exception("close failed during hot_unplug of %s", iface.name)
        self._member_lost(iface, None, close=False)   # already closed above

    def _member_lost(self, iface: WlanInterface, exc: Optional[Exception], *,
                     close: bool = True) -> None:
        """Drop a member (unplug or disconnect) and re-emit with the surviving card count so a
        caller can route: 0 -> back to splash, 1+ -> toast and keep running. ``close`` shuts the dead
        card down to stop its async tasks; hot_unplug passes False since it already closed."""
        if iface in self._members:
            self._members.remove(iface)
        if iface is self._preferred:
            self._preferred = None      # pinned card unplugged: fall back to auto until re-picked
        self._dedupe.remove_source(iface.name)
        remaining = len(self._members)
        logger.info("lost %s; %d card(s) remain", iface.name, remaining)
        if close:
            self._close_lost(iface)   # stop the dead card's async tasks (watchdog / RX / hop)
        if self._hopping and remaining:
            self._repartition()   # survivors re-cover the departed card's channels
        for cb in list(self._disconnect_callbacks):
            try:
                cb(exc, remaining)
            except Exception:
                logger.exception("Array disconnect callback failed")

    # ----- card selection ----------------------------------------------------

    @property
    def preferred(self) -> Optional[WlanInterface]:
        return self._preferred

    def prefer(self, iface: Optional[WlanInterface]) -> None:
        """Pin a card as the session TX pick. ``select_iface`` returns it for any target it can
        reach; None restores pure capability ranking. In-memory only (a replug is a fresh object,
        so it re-picks). Cleared automatically when the pinned card is lost."""
        self._preferred = iface

    def select_iface(self, channel: int) -> Optional[WlanInterface]:
        """The card to TX on for ``channel``: the user's pinned card when it can reach the band,
        else the most attack-capable card that can. None when no live card can reach the band at
        all (e.g. a 5 GHz target with only 2.4 GHz cards left)."""
        cands = [m for m in self._members if channel in m.supported_channels]
        if not cands:
            return None
        if self._preferred in cands:
            return self._preferred
        return min(cands, key=fake_mac_rank)

    def lease(self, channel=None, fake_mac=None, bssid=None, ack_tally=False, iface=None) -> Lease:
        """Scoped hold on one interface (selected by ``channel`` unless ``iface`` is given).
        Restores channel, fake MAC and ACK tally on exit; raises when no interface can serve."""
        target = iface
        if target is None and channel is not None:
            target = self.select_iface(channel)
        if target is None:
            target = self._members[0] if self._members else None
        if target is None:
            raise RuntimeError("no interface available to lease")
        return Lease(self, target, channel=channel, fake_mac=fake_mac,
                     bssid=bssid, ack_tally=ack_tally)

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
        copy into the shared 802.11 state (every card still contributes its own signal on a dup)."""
        card_id = iface.name
        # Drop frames WE transmitted, keyed on the transmitter (Addr2/TA), NOT pkt.source: on a
        # FromDS frame source is addr3, so the AP's fresh-IV rebroadcast of our replayed ARP (our MAC
        # in addr3, BSSID as TA) would be dropped and zero the IV rate. TA-keying counts it and still
        # drops a second card hearing our own ToDS injection (TA == our MAC).
        if pkt.transmitter in self._sink.own_macs:
            return
        if self._is_stray_beacon(pkt):
            return
        if self._dedupe.submit(card_id, pkt.raw, time.monotonic()):
            self._sink.update(pkt, card_id, channel_hint=iface.current_channel)
            self._sink.dispatch_rx(pkt)
            for cb in self._rx_callbacks:
                try:
                    cb(pkt)
                except Exception:
                    logger.exception("Deduped RX callback failed")
        else:
            self._sink.record_signal(card_id, pkt.bssid, pkt.rssi)

    def ignore_stray_beacons(self, bssid: str, channel: int) -> None:
        """Drop this BSSID's beacons/probe-resps on ``channel``."""
        self._stray_beacon_channels[bssid] = channel

    def stop_ignoring_stray_beacons(self, bssid: str) -> None:
        self._stray_beacon_channels.pop(bssid, None)

    def _is_stray_beacon(self, pkt: Packet) -> bool:
        return (isinstance(pkt, BeaconPacket)
                and self._stray_beacon_channels.get(pkt.bssid) == pkt.channel)

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

    async def next_frame(self, match, timeout: float):
        return await self._sink.next_frame(match, timeout)

    async def wait_until(self, condition, timeout: float, poll: float = 0.05) -> bool:
        return await self._sink.wait_until(condition, timeout, poll)

    def get_access_points(self, include_eviltwin: bool = True) -> List[AccessPoint]:
        aps = self._sink.get_access_points()
        if include_eviltwin:
            return aps
        return [ap for ap in aps if ap.bssid not in self._evil_twin_bssids]

    def register_forged_mac(self, mac) -> None:
        self._sink.register_forged_mac(mac)

    def register_own_mac(self, mac) -> str:
        return self._sink.register_own_mac(mac)

    def unregister_own_mac(self, mac) -> None:
        self._sink.unregister_own_mac(mac)

    def record_injected_eapol(self, frame) -> None:
        self._sink.record_injected_eapol(frame)

    def register_self_mac(self, mac, bssid: Optional[str] = None) -> str:
        return self._sink.register_self_mac(mac, bssid)

    def unregister_self_mac(self, mac) -> None:
        self._sink.unregister_self_mac(mac)

    def mark_evil_twin(self, bssid: str) -> None:
        """Hide this BSSID's AP from get_access_points(include_eviltwin=False): it's our own twin."""
        self._evil_twin_bssids.add(bssid.lower())

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
        """STACK: tune every channel-capable member to ``channel`` (focus/PBC), one at a time. A card
        that can't reach it is skipped; a card already on it counts as success (the postcondition
        holds); a card that fails to tune is logged and the rest still tune. Returns True if at least
        one card is on ``channel``."""
        tuned_any = False
        for m in self._members:
            if channel not in m.supported_channels:
                continue
            if m.current_channel == channel:
                tuned_any = True                    # already there: it IS on the channel
                continue
            try:
                if await m.set_channel(channel, scan=scan):
                    tuned_any = True
            except Exception:
                logger.exception("%s failed to tune to channel %d", m.name, channel)
        return tuned_any

    def _partition(self, channels: List[int]) -> dict:
        """SPREAD: give each channel to one capable card, balancing counts, so N cards cover N-way
        more air per hop. Iterate channels high-first (5 GHz before 2.4 GHz) so a dual-band card
        absorbs the scarce 5 GHz work before the all-band 2.4 GHz channels spread. That keeps cards
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

    def _run_on_loop(self, spawn) -> None:
        """Call ``spawn(loop)`` on the array's event loop: directly when already on it (attach), or
        via call_soon_threadsafe when not (the RX-reader disconnect path runs off the loop)."""
        loop = self._loop
        if loop is None:
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            spawn(loop)
        else:
            loop.call_soon_threadsafe(spawn, loop)

    def _repartition(self) -> None:
        """Re-run the hop partition for the current membership (a card joined or left while hopping)."""
        def _spawn(loop) -> None:
            task = loop.create_task(self._rehop())
            self._rehop_tasks.add(task)
            task.add_done_callback(self._rehop_tasks.discard)
        self._run_on_loop(_spawn)

    async def _rehop(self) -> None:
        try:
            await self.start_hopping(self._hop_channels, self._hop_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("re-hop after membership change failed")

    def _close_lost(self, iface: WlanInterface) -> None:
        """Stop a vanished card's async tasks (watchdog, RX reader, hop) by closing it. The device is
        gone, so close() may itself raise doing USB ops on a dead handle, so swallow everything. Runs on
        the array's loop (_member_lost fires off the RX-reader thread)."""
        async def _close() -> None:
            try:
                await iface.close()
            except Exception:
                logger.debug("close of lost %s raised (device gone)", iface.name, exc_info=True)

        def _spawn(loop) -> None:
            task = loop.create_task(_close())
            self._close_tasks.add(task)
            task.add_done_callback(self._close_tasks.discard)
        self._run_on_loop(_spawn)

    async def close(self) -> None:
        await asyncio.gather(*(m.close() for m in self._members), return_exceptions=True)
        self._members.clear()
