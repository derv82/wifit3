import asyncio
import logging
import struct
import json
import time
import usb.core
import usb.util
from typing import Dict
from pathlib import Path

from .transport import AR9271USBTransport
from .protocol.wmi import WMIProtocol
from .protocol.metadata import AthMetadataLayer
# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *
from wifit3.engine.protocols import DeviceID, FakeMacSupport

logger = logging.getLogger(__name__)

# Atheros own-MAC registers — the RA the hardware matches to auto-ACK.
AR_STA_ID0 = 0x8000   # [SRC] ath9k reg.h: MAC bytes 0-3 (LE)
AR_STA_ID1 = 0x8004   # [SRC] ath9k reg.h: bytes 4-5 (LE) in [15:0], opmode/KSRCH in [31:16]


class AR9271Driver:
    """
    Main Driver for the Atheros AR9271.
    Orchestrates protocols and device state using the USBTransport abstraction.
    """

    SUPPORTED_IDS = [
        DeviceID(0x0cf3, 0x9271, "Atheros AR9271 (ALFA AWUS036NHA)"),
    ]
    # 2.4 GHz only (no 5 GHz radio).
    SUPPORTED_CHANNELS = list(range(1, 14))
    CONFLICTING_LINUX_MODULES = ["ath9k_htc"]   # modprobe blacklist hint (live probe is authoritative)
    LINUX_REPLUG_AFTER_MODPROBE = False   # self-colds: FW download re-enumerates
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "AR9271Driver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device, is_warm: bool = False):
        # Initial transport for whatever handle we were given (Cold or Warm)
        self.transport = AR9271USBTransport(dev)
        self.wmi = WMIProtocol()
        self.meta = AthMetadataLayer()
        self.is_warm = is_warm
        
        # Handshake Events
        self._htc_ready_event = asyncio.Event()
        self._htc_config_done_event = asyncio.Event()
        self._wmi_ready_event = asyncio.Event()
        
        # Mapping for ACKs and WMI Events
        self._event_queues: Dict[int, asyncio.Queue] = {}
        
        # State tracking
        self.wmi_endpoint_id = HTC_ENDPOINT_WMI
        self.data_endpoint_id = 5
        self.mac_address = None
        self.total_credits = 0
        self._rx_callback = None

        # Observe the AP's ACK to our injects (did our TX land). Off by default.
        self._ack_detect_on: bool = False
        self._our_tx_macs: set[bytes] = set()      # source MACs we inject as
        self._ack_sightings: dict[str, int] = {}   # our-MAC -> ACK count
        self._all_acks_seen: int = 0
        self._ack_last_ts: dict[bytes, float] = {}  # our-MAC -> ts of last ACK
        self._tx_frames: int = 0
        self._tx_unacked: int = 0

        # EP 0 for HTC/WMI Management, EP 1 for WMI Commands/Events
        self.transport.subscribe(0, self._on_htc_control)

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    async def connect(self, progress_cb=None):
        """
        Performs the event-driven HTC/WMI handshake.
        Handles COLD -> WARM transition if necessary.
        """
        def _update(pct, msg):
            if progress_cb:
                progress_cb(pct, msg)
            logger.info(f"Progress {int(pct*100)}%: {msg}")

        # 1. Single check for warmth (skip if caller already asserted warmth)
        if not self.is_warm:
            _update(0.05, "Probing hardware state...")
            self.is_warm = await self._check_if_warm()
        
        if not self.is_warm:
            _update(0.1, "AR9271 is COLD. Initiating firmware upload...")
            from .firmware import FirmwareLoader
            
            # Find the fw file relative to the driver directory
            fw_path = Path(__file__).parent / "assets" / "htc_9271_cleanroom.fw"
            with open(fw_path, 'rb') as f:
                fw_bytes = f.read()
                
            # load() handles dispose_resources(dev) internally
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, FirmwareLoader.load, self.transport.dev, fw_bytes)
            if not success:
                logger.error("Firmware upload failed.")
                return False
                
            _update(0.15, "Waiting for AR9271 to re-enumerate...")
            warm_dev = None
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
            
            for i in range(12): # 3 second total wait
                await asyncio.sleep(0.25)
                warm_dev = usb.core.find(idVendor=0x0cf3, idProduct=0x9271, backend=backend)
                if warm_dev:
                    break
                _update(0.15 + (i * 0.01), f"Waiting for re-enumeration... ({i+1}/12)")
                
            if not warm_dev:
                logger.error("AR9271 failed to re-enumerate after boot.")
                return False
                
            _update(0.3, "AR9271 successfully warmed up. Re-initializing transport...")
            
            # Create a FRESH transport and reset events for the new handle
            self.transport = AR9271USBTransport(warm_dev)
            self.transport.subscribe(0, self._on_htc_control)
            
            self._htc_ready_event = asyncio.Event()
            self._htc_config_done_event = asyncio.Event()
            self._wmi_ready_event = asyncio.Event()
            self._event_queues = {}
            
            # We are no longer initially warm; we must do the full handshake
            self.is_warm = False 

        if self.is_warm:
            # OPTIMIZATION: Fast re-attach for already-running firmware
            _update(0.4, "Device is already WARM. Re-attaching to existing HTC/WMI streams...")
            self.total_credits = 33 # Assume default from previous handshake
            self.wmi_endpoint_id = HTC_ENDPOINT_WMI # WMI is mapped to HTC endpoint 1
            self.data_endpoint_id = 5 # Default Data EP
            
            # Seed the CreditManager to prevent deadlocks since we skipped the HTC_READY handshake
            self.transport.credit_manager.set_initial(self.wmi_endpoint_id, self.total_credits)
            self.transport.credit_manager.set_initial(self.data_endpoint_id, self.total_credits)
            
            self._htc_ready_event.set()
            self._htc_config_done_event.set()
            self._wmi_ready_event.set()
            
            # We MUST clear the halt/toggle bits on the pipes so Bulk OUT doesn't drop packets
            self.transport.reset_pipes()
            await asyncio.sleep(0.05)
            
            await self.transport.start()
            
            # Subscribe to the data streams
            # Subscribe to Management (0), WMI (1), and CAB (3) for the Bulk stream
            self.transport.subscribe(0, self._on_wmi_packet)
            self.transport.subscribe(1, self._on_wmi_packet)
            self.transport.subscribe(3, self._on_wmi_packet)
            _update(1.0, "AR9271 Driver successfully re-attached.")
            return True

        # --- FULL HANDSHAKE (The Final Marathon) ---
        _update(0.4, "Starting AR9271 Handshake (Final Marathon)...")
        
        await self.transport.start()
        
        _update(0.45, "Waiting for HTC Ready...")
        try:
            await asyncio.wait_for(self._htc_ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for HTC Ready signal.")
            return False

        _update(0.5, "Connecting WMI services...")
        # WMI Service (0x0100) -> DL=3, UL=4
        wmi_payload = struct.pack(">HHHBBBB", 0x0002, 0x0100, 0, 3, 4, 0, 0)
        await self.transport.send(0, wmi_payload, is_wmi=False)
        await asyncio.sleep(0.001)
        
        others = [0x0101, 0x0102, 0x0103, 0x0104, 0x0107, 0x0108, 0x0106, 0x0105]
        for svc_id in others:
            payload = struct.pack(">HHHBBBB", 0x0002, svc_id, 0, 2, 1, 0, 0)
            await self.transport.send(0, payload, is_wmi=False)
            await asyncio.sleep(0.001)

        _update(0.55, "Configuring HTC Pipe...")
        config_pipe = struct.pack(">HBB", 0x0005, 1, self.total_credits)
        await self.transport.send(0, config_pipe, is_wmi=False)
        await asyncio.sleep(0.005)

        _update(0.6, "Completing HTC Handshake...")
        setup_done = struct.pack(">H", 0x0004)
        await self.transport.send(0, setup_done, is_wmi=False)
        
        self.transport.reset_pipes()
        await asyncio.sleep(0.05) # Half-breath

        # 6. Ultimate Calibration Marathon
        # We offload this tightly-coupled loop to a background thread to prevent UI freezing.
        loop = asyncio.get_running_loop()
        success = await asyncio.to_thread(self._replay_calibration_sync, _update, loop)
        if not success:
            return False

        _update(0.98, "Waiting for Target Ready and MAC verification...")
        try:
            # Accelerated wait: v1.4 usually responds within 500ms post-marathon
            await asyncio.wait_for(self._wmi_ready_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for WMI Target Ready event. Proceeding anyway...")
        
        # Establish the monitor RX filter (PROM) so passive frames are captured.
        # The replayed init doesn't reliably hold it; set it explicitly here.
        await self._apply_monitor_rx_filter()

        # Seed the DATA endpoint's credits, exactly as the warm path does
        # (see the is_warm branch above). The HTC READY handshake only grants
        # initial credits to EP 1 and EP 2 (transport._process_htc_frame), and
        # the device only *replenishes* an endpoint after the host has consumed
        # a credit on it — so EP 5 (our bulk-OUT data/inject path) sits at 0
        # forever on cold boot. The first inject_frame() then blocks in
        # credit_manager.acquire(5) → cold-boot PMKID/deauth hangs while RX
        # (which uses no credits) stays fine. Seeding here mirrors the proven
        # warm path and lets TX proceed immediately.
        self.transport.credit_manager.set_initial(self.data_endpoint_id, self.total_credits)

        _update(1.0, f"AR9271 Driver successfully connected. MAC: {self.mac_address}")
        return True

    def _replay_calibration_sync(self, update_cb, loop) -> bool:
        """
        Synchronous version of the 1,200-packet calibration marathon.
        Offloaded to a background thread to keep the UI fluid.
        """
        update_cb(0.65, "Replaying 1,200-Packet Calibration Table...")
        # Subscribe to Management (0), WMI (1), and CAB (3) for the Bulk stream
        self.transport.subscribe(0, self._on_wmi_packet)
        self.transport.subscribe(1, self._on_wmi_packet)
        self.transport.subscribe(3, self._on_wmi_packet)
        
        cal_path = Path(__file__).parent / "assets" / "ar9271_v14_init.json"
        try:
            with open(cal_path, 'r') as f:
                payloads = json.load(f)
            
            total_payloads = len(payloads)
            
            for i, p_hex in enumerate(payloads):
                raw_wmi = bytes.fromhex(p_hex)
                cmd_id, p_seq = struct.unpack(">HH", raw_wmi[:4])
                payload_data = raw_wmi[4:]
                
                # Robust retry loop for calibration packets
                success = False
                for attempt in range(3):
                    try:
                        # We must bridge back to the event loop for the async transport send/wait
                        future = asyncio.run_coroutine_threadsafe(
                            self.send_wmi_command(cmd_id, payload_data, wait_for_ack=True), 
                            loop
                        )
                        success = future.result()
                        if success:
                            break
                    except Exception as e:
                        logger.warning(f"Error at packet {i} (Attempt {attempt}): {e}")
                        self.transport.reset_pipes()
                        time.sleep(0.01)
                
                if not success:
                    logger.warning(f"Failed to process packet {i} (Cmd {hex(cmd_id)}).")
                    
                if i > 0 and i % 100 == 0:
                    prog = 0.65 + (0.3 * (i / total_payloads))
                    update_cb(prog, f"Replaying registers... ({i}/{total_payloads})")
                    
            update_cb(0.95, "Calibration complete. Triggering Radio Init...")
            
            # Final Trigger Sequence
            asyncio.run_coroutine_threadsafe(self.send_wmi_command(WMI_SET_MODE_CMDID, struct.pack(">H", 0x0001)), loop).result()
            asyncio.run_coroutine_threadsafe(self.send_wmi_command(0x0006, b""), loop).result()
            asyncio.run_coroutine_threadsafe(self.send_wmi_command(WMI_START_RECV_CMDID, b""), loop).result()
            
            return True
            
        except FileNotFoundError:
            logger.error(f"Calibration file not found at {cal_path}")
            return False
        except Exception as e:
            logger.error(f"Calibration marathon failed: {e}")
            return False

    async def _check_if_warm(self) -> bool:
        """
        Probes the device to see if firmware is already active.
        Based on the last known good 'midnight' logic.
        """
        try:
            # We use the current handle to try a short bulk read.
            # If the firmware is running, it will either return data or timeout.
            # If it's a BootROM, it might Pipe Error or timeout.
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self.transport.dev.read, 0x82, 512, 100)
            # If we got any data, it's definitely warm.
            return len(data) > 0
        except Exception:
            # Any error (including timeout) means we assume it's cold or unresponsive.
            return False

    def _on_htc_control(self, payload: bytes):
        """Callback for EP 0 packets arriving on Interrupt pipe (0x83)."""
        if len(payload) < 2:
            return
        msg_id = struct.unpack(">H", payload[:2])[0]
        if msg_id == 0x0001: # READY
            credits = struct.unpack(">H", payload[2:4])[0]
            self.total_credits = credits
            self._htc_ready_event.set()
        elif msg_id == 0x0003: # CONNECT_RSP
            svc_id = struct.unpack(">H", payload[2:4])[0]
            status, epid = struct.unpack(">BB", payload[4:6])
            if status == 0:
                if svc_id == 0x0100:
                    self.wmi_endpoint_id = epid
                logger.debug(f"Service {hex(svc_id)} connected on EP {epid}")
        elif msg_id == 0x0006: # CONFIG_PIPE_RSP
            self._htc_config_done_event.set()

    def _on_wmi_packet(self, payload: bytes):
        """Callback for WMI and Management packets arriving on Bulk pipe (0x82)."""
        try:
            ev_id, seq, wmi_payload = self.wmi.unpack_event(payload)
            
            # 1. ACK matching for synchronous commands
            if seq in self._event_queues:
                self._event_queues[seq].put_nowait((ev_id, wmi_payload))
                return
            
            # 2. Async Target Ready - can arrive on EP 0 or 1
            if ev_id == WMI_READY_EVENTID or ev_id == WMI_READY_EP0_ID:
                self.mac_address = ":".join(f"{b:02x}" for b in wmi_payload[:6])
                logger.info(f"[!!!] AR9271 SILICON MAC VERIFIED: {self.mac_address}")
                self._wmi_ready_event.set()
                
            # 3. Handle live RX traffic (Beacons, Data, etc.)
            elif ev_id in [WMI_RECV_PDU_EVENTID, WMI_RECV_PDU_V14_ID, WMI_RECV_PDU_V14_BCN_ID]:
                if self._ack_detect_on:
                    ra = self.wmi.ack_ra(wmi_payload)
                    if ra is not None:
                        self._all_acks_seen += 1
                        if ra in self._our_tx_macs:
                            self._ack_sightings[ra.hex()] = self._ack_sightings.get(ra.hex(), 0) + 1
                            self._ack_last_ts[ra] = time.monotonic()  # for inject wait-for-ack
                        return                # an ACK is never handed to the frame parser
                parsed = self.wmi.parse_rx_frame(wmi_payload)
                if parsed and self._rx_callback:
                    self._rx_callback(parsed)
            
            elif ev_id & 0x1000:
                logger.debug(f"Async WMI Event: ID={hex(ev_id)} LEN={len(wmi_payload)}")
                
        except Exception as e:
            logger.debug(f"WMI Unpack fail: {e}")

    async def send_wmi_command(self, command_id: int, payload: bytes, wait_for_ack: bool = True):
        wmi_payload, seq = self.wmi.pack_command(command_id, payload)
        if wait_for_ack:
            ack_queue = asyncio.Queue()
            self._event_queues[seq] = ack_queue
            
        await self.transport.send(self.wmi_endpoint_id, wmi_payload, is_wmi=True)
        
        if wait_for_ack:
            try:
                # Register ACKs are near-instant (<5ms)
                res_ev_id, res_payload = await asyncio.wait_for(ack_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                logger.debug(f"Timeout waiting for ACK on Cmd {hex(command_id)}. Resending to sync toggle bit...")
                # The hardware often ignores clear_halt(), leaving the USB toggle bit desynced.
                # The first packet is dropped, but the host advances its toggle bit.
                # Resending it forces a match!
                await self.transport.send(self.wmi_endpoint_id, wmi_payload, is_wmi=True)
                try:
                    res_ev_id, res_payload = await asyncio.wait_for(ack_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    return False
            finally:
                if seq in self._event_queues:
                    del self._event_queues[seq]
                    
        return True

    async def _apply_monitor_rx_filter(self) -> None:
        """Write AR_RX_FILTER with the PROM (promiscuous) bit so we capture
        other stations' frames (passive 4-way handshakes), not just frames
        directed at us. ath9k_htc does this whenever monitoring AND re-applies
        it after every reset/channel-change (which re-runs the INI tables and
        clears PROM) — so we call it after init and after each set_channel.
        Uses the existing WMI register-write primitive; payload is
        [addr:u32 BE][val:u32 BE]. [SRC ath9k htc_drv_txrx.c:884]
        """
        await self.send_wmi_command(
            WMI_REG_WRITE_CMDID,
            struct.pack(">II", AR_RX_FILTER, RX_FILTER_MONITOR),
        )

    async def set_channel(self, channel: int, scan: bool = False):
        from .sequences.tuning import get_channel_hop_sequence
        logger.info(f"Tuning AR9271 to Channel {channel}...")
        sequence = get_channel_hop_sequence(channel)
        for cmd_id, payload in sequence:
            success = await self.send_wmi_command(cmd_id, payload)
            if not success:
                return False
        # The channel change re-runs the INI tables (PROM cleared); re-apply the
        # monitor RX filter, matching ath9k_htc's post-reset behaviour.
        await self._apply_monitor_rx_filter()
        return True

    async def enable_ack_detect(self) -> None:
        """Arm the ACK tap. Pure software flag — the monitor RX filter (RX_FILTER_MONITOR 0xC03F)
        already sets ATH9K_RX_FILTER_CONTROL, so the firmware forwards ACK control frames; no
        register write needed. Not enter_active_monitor, which makes the chip emit ACKs."""
        self._ack_sightings.clear()
        self._ack_last_ts.clear()
        self._all_acks_seen = 0
        self._tx_frames = 0
        self._tx_unacked = 0
        self._ack_detect_on = True
        logger.info("AR9271 TX-ACK detection ON — observing our TX delivery")

    async def disable_ack_detect(self) -> None:
        """Disarm the ACK tap (software flag only; the RX filter stays at the monitor default)."""
        self._ack_detect_on = False

    def acks_seen(self, mac: bytes) -> int:
        """Count of ACKs observed addressed to ``mac`` (an injected source MAC) since enable."""
        return self._ack_sightings.get(bytes(mac).hex(), 0)

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True,
                           wait_for_ack: float = 0.0, max_resends: int = 0) -> bool:
        """
        Injects a raw 802.11 frame onto the air.
        Wraps the frame in the `ath_tx_status` hardware descriptor.

        ``wait_for_ack > 0`` (with TX-ACK detection armed) waits for the AP's ACK and resends the
        same frame up to ``max_resends`` times if none comes, returning whether it landed; ``0`` =
        fire-and-forget (byte-identical to the prior behaviour).
        """
        ta = bytes(frame_bytes[10:16]) if len(frame_bytes) >= 16 else None   # AP ACKs back to TA
        if self._ack_detect_on and ta is not None:
            self._our_tx_macs.add(ta)
        # Pack the hardware TX descriptor (rate 0x0B, no_ack flag)
        tx_payload = self.meta.pack_tx(frame_bytes, rate_idx=0x0B, no_ack=use_no_ack)

        # Dispatch to the dynamic Data endpoint (usually EP 5)
        # is_wmi=False means it uses the standard 8-byte HTC header, not the WMI variant
        # is_data=True means it must go to Bulk OUT (EP 0x01) with a 4-byte HIF header!
        ack_gated = wait_for_ack > 0 and self._ack_detect_on and ta is not None
        for _ in range(max_resends + 1):
            t0 = time.monotonic()
            await self.transport.send(self.data_endpoint_id, tx_payload, is_wmi=False, is_data=True)
            self._tx_frames += 1
            if not ack_gated:
                return True                 # fire-and-forget (deauth / WEP / current behaviour)
            if await self._await_ack(ta, t0, wait_for_ack):
                return True                 # landed — the AP ACKed it
        self._tx_unacked += 1
        return False                        # never ACKed after every send

    async def _await_ack(self, ta: bytes, since: float, window: float) -> bool:
        """True if the tap observed an ACK to ``ta`` after ``since``, within ``window`` s.
        _on_wmi_packet runs on this loop, so a sleep yield lets a just-arrived ACK's timestamp
        land between checks."""
        deadline = since + window
        while time.monotonic() < deadline:
            if self._ack_last_ts.get(ta, 0.0) > since:
                return True
            await asyncio.sleep(0.001)
        return False

    async def enter_active_monitor(self, mac: bytes, bssid=None) -> bytes:
        """Program ``mac`` into AR_STA_ID0/1 so the hardware HW-ACKs frames to it (ath9k
        matches RA against AR_STA_ID; our cold path leaves the firmware's real MAC there).
        Reversed by exit_active_monitor."""
        await self._write_sta_id(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real MAC in AR_STA_ID0/1."""
        if self.mac_address:
            await self._write_sta_id(bytes(int(b, 16) for b in self.mac_address.split(":")))

    async def _write_sta_id(self, mac: bytes) -> None:
        # AR_STA_ID1 upper 16 (opmode/KSRCH) cleared — monitor keeps no such state.
        await self.send_wmi_command(
            WMI_REG_WRITE_CMDID, struct.pack(">II", AR_STA_ID0, int.from_bytes(mac[0:4], "little")))
        await self.send_wmi_command(
            WMI_REG_WRITE_CMDID, struct.pack(">II", AR_STA_ID1, int.from_bytes(mac[4:6], "little")))

    async def close(self):
        await self.transport.stop()
        logger.info("AR9271 Driver closed.")
