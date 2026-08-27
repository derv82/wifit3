import logging
from pathlib import Path
from typing import Optional, Callable

import usb.core

from . import init as chip_init
from . import mcu, rx, tx
from .transport import MT7921AUTransport
from .firmware import MT7921AUFirmwareLoader
# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.products import ALFA, Panda
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

logger = logging.getLogger(__name__)


class MT7921AUDriver(Driver):
    """Userspace driver for the MediaTek MT7921AU (Wi-Fi 6).

    Bring-up state (see chips/mt7921au/MT7921AU.md): firmware boot (firmware.py),
    post-boot device init (init.py / mac.py / mcu.py), monitor entry, channel
    tune, RX descriptor decode and TX (inject_frame) are ported, pcap-verified
    (verify_pcap CHECK 1-4) and HW-confirmed (both bands + the full attack suite).

    A warm chip (firmware still running from a prior run) is detected via
    MT_CONN_ON_MISC/FW_N9_RDY and LIGHT-reattached (connect → _warm_reattach), the
    kernel mt7921u_resume model: no reset, no firmware reload — just re-post RX and
    re-sync the channel. Cold chips take the full _cold_boot path.
    """

    # Dual-band Wi-Fi 6 radio, 20 MHz primary. 2.4 GHz (1-14) + the 5 GHz 20 MHz
    # channels of the world regulatory domain (regdomain.CHANNELS_5GHZ).
    SUPPORTED_CHANNELS = list(range(1, 15)) + [
        36, 40, 44, 48, 149, 153, 157, 161, 165,
    ]
    # Bench (rx_autoack, 2026-07-16): auto-ACKs a spoofed MAC via active monitor on both
    # bands (2G 102/100, 5G 100/100); does NOT ACK its own silicon MAC. Behaves SPOOFABLE.
    # (An earlier read of ~120 EAPOL per WPS PBC and openwrt/mt76#839 suggested otherwise;
    # the direct auto-ACK bench overrides it.)
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    # Device setup installs the udev rule + modprobe blocklist, but neither applies until the next
    # device-add — until then the kernel's mt7921u still owns the interface and our claim/control
    # transfers EACCES. Auto-connecting into that fails before the driver gets a clean shot. So gate
    # on a replug: after the rules land, the card re-enumerates cold (kernel blocklisted, udev rule
    # live) and our cold boot runs on a pristine chip. See MT7921AU.md. (Cold/normal plug unaffected.)
    LINUX_REPLUG_AFTER_MODPROBE = True

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7921AUDriver":
        drv = cls(dev)
        drv.product_name = id_entry.product_name   # the Splash/SUPPORTED_IDS label; connect() narrows by OUI
        return drv

    def __init__(self, dev):
        super().__init__()   # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        self.transport = MT7921AUTransport(dev)
        self.firmware = MT7921AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._init_state: Optional[chip_init.InitState] = None
        self._channel = self.SUPPORTED_CHANNELS[0]
        # is_warm reflects the bring-up path taken by connect(): True when we light-reattached
        # to already-running firmware (_warm_reattach), False on a cold boot. mac_address is
        # parsed from the GET_NIC_CAPAB reply during cold boot (MT_NIC_CAP_MAC_ADDR TLV); it
        # stays None on a warm reattach, which skips post-boot init.
        self.is_warm: bool = False
        self.mac_address: Optional[str] = None
        self._nic_has_6ghz: int = 0
        # Per-card antenna_mask (chains the RX RSSI loop reads + SET_RX_PATH streams),
        # derived from the GET_NIC_CAPAB PHY cap on cold boot. 0x3 = the captured 2x2
        # reference; a warm reattach (no post-boot init) keeps this default.
        self._antenna_mask: int = 0x3
        # 802.11 TX sequence counter (number in seq_ctrl bits [4:15], so it steps
        # by 0x10). The chip transmits the seq we stamp (TXD SN_VALID), so we own
        # it (see tx.stamp_seq_ctrl). Touched on the event loop only (no lock).
        self._tx_seqno: int = 0

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). The transport owns the reader, so
        forward it there; picked up when start_rx builds the RxReaderThread."""
        self.transport._on_fatal = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Bring up monitor mode — cold-boot the firmware, or LIGHT-reattach if it is
        already running (warm).

        The kernel's mt7921u_probe reads MT_CONN_ON_MISC/FW_N9_RDY to tell whether
        firmware is already up; that read is HW-safe here. A warm chip still has
        firmware running in monitor mode, so we reattach to it (the mt7921u_resume
        model) instead of cold-booting. A cold boot's wfsys_reset + mcu_power_on
        POISON a warm chip's bulk pipes on WinUSB (where we cannot do the kernel's
        pre-reset usb_reset_device), so warm and cold are strictly separate paths.
        See chips/mt7921au/MT7921AU.md "Warm re-attach"."""
        # ProgressCallback is (percentage, message) — the wifit3-wide convention.
        if progress_cb:
            progress_cb(0.1, "Connecting to MT7921AU...")
        logger.info("Initializing MT7921AU...")
        self.transport.subscribe(self._on_raw_rx)

        if self._detect_warm():
            return await self._warm_reattach(progress_cb)
        return await self._cold_boot(progress_cb)

    def _detect_warm(self) -> bool:
        """True if firmware is already running (FW_N9_RDY set in MT_CONN_ON_MISC) —
        the kernel mt7921u_probe warm check. Claims the vendor interface (needed for
        register access) but does NOT clear-halt or reset: a warm chip must not be
        cold-booted. Reading MT_CONN_ON_MISC is HW-verified safe on this chip.
        Returns False (treat as cold) if the read fails."""
        self.firmware._claim_vendor_interface(clear_halts=False)
        try:
            misc = self.transport.read_reg32_unified(MT_CONN_ON_MISC)
        except usb.core.USBError:
            return False
        logger.debug("MT7921AU warm-check: MT_CONN_ON_MISC=0x%x", misc)
        return (misc & MT_TOP_MISC2_FW_N9_RDY) != 0

    @staticmethod
    def derive_product_name(mac: Optional[str]) -> Optional[str]:
        """AXML and PAU0F ship as one VID:PID; the burned-in OUI is the only tell.
        (ALFA 00:c0:ca:ba:4e:91, Panda 9c:ef:d5:f6:44:a4.) None when the MAC is unknown."""
        if not mac:
            return None
        oui = mac[:8].lower()
        if oui == "00:c0:ca":
            return ALFA.AWUS036AXML
        if oui == "9c:ef:d5":
            return Panda.PAU0F
        return None

    def _refine_product_name(self) -> None:
        """Narrow product_name past the shared SUPPORTED_IDS default once the MAC is read. A MAC we
        can't place (unknown OUI, or None on a query miss) leaves the default untouched."""
        refined = self.derive_product_name(self.mac_address)
        if refined:
            self.product_name = refined

    async def _cold_boot(self, progress_cb: Optional[ProgressCallback]) -> bool:
        """Full bring-up of a cold chip: firmware upload, post-boot device init,
        monitor entry. The RX reader is started by load_firmware."""
        if progress_cb:
            progress_cb(0.1, "Uploading firmware...")
        if not await self.firmware.load_firmware():
            raise BringUpError("firmware", "MT7921AU firmware load failed")
        self.transport.start_rx()   # idempotent — load_firmware already started it

        if progress_cb:
            progress_cb(0.6, "Configuring device...")
        logger.debug("Running MT7921AU post-boot init...")
        self._init_state = await chip_init.post_boot_init(self.transport)
        caps = self._init_state.caps
        self.mac_address = caps.mac
        self._refine_product_name()
        self._antenna_mask = caps.antenna_mask
        self._nic_has_6ghz = int(caps.has_6ghz)
        self._log_nic_caps(caps)

        # Enter monitor mode on the initial channel (the RX reader routes the
        # monitor commands' acks back, and 802.11 frames to _on_raw_rx).
        if progress_cb:
            progress_cb(0.9, "Enabling monitor mode...")
        await chip_init.enter_monitor(self.transport, self._channel)

        self.is_warm = False
        if progress_cb:
            progress_cb(1.0, "Done")
        logger.info("MT7921AU monitor mode ready (cold boot) on channel %d.", self._channel)
        return True

    def _log_nic_caps(self, caps: mcu.NicCaps) -> None:
        """Log the per-card GET_NIC_CAPAB config the cold bring-up branched on, once.
        Tagged '[untested variant]' when the caps differ from the captured pau0f/AXML
        reference (antenna_mask 0x3, 2.4+5+6 GHz) — such a card runs the ported runtime
        derivation give-it-a-shot, with no cold-boot capture to gate it."""
        n = bin(caps.antenna_mask).count("1")
        summary = (f"MAC {caps.mac}, antenna_mask 0x{caps.antenna_mask:x} ({n}x{n}), "
                   f"bands 2.4={int(caps.has_2ghz)} 5={int(caps.has_5ghz)} 6={int(caps.has_6ghz)}")
        if caps.is_reference:
            logger.debug("MT7921AU NIC caps: %s", summary)
        else:
            logger.warning("MT7921AU NIC caps [untested variant]: %s (reference: "
                           "antenna_mask 0x3, all bands). Running the ported runtime "
                           "derivation.", summary)

    async def _warm_reattach(self, progress_cb: Optional[ProgressCallback]) -> bool:
        """Light reattach to firmware that is already running in monitor mode — the
        kernel mt7921u_resume model. NO reset, NO mcu_power_on, NO firmware reload, NO
        post-boot init: the firmware already did all of that and keeps streaming RX
        the instant we re-post a read. We only re-establish the host interface:

          - a light dma_init(resume) IFF the WFDMA NEED_REINIT latch was cleared
            (mt792x_dma_need_reinit); a normal reconnect leaves it set, so skipped.
          - re-post RX (start the reader)  == mt76u_resume_rx
          - re-sync the channel.

        set_hif_suspend(false) is intentionally omitted: the kernel sends it on PM
        resume because IT called set_hif_suspend(true) on suspend. Our cross-process
        reconnect never suspended the HIF (RX streams immediately on reattach), so
        there is nothing to un-suspend — a justified exception (the kernel has no
        analog for a fresh-handle reconnect to never-suspended firmware)."""
        logger.info("MT7921AU warm reattach (firmware already running)...")
        if progress_cb:
            progress_cb(0.5, "Reattaching to running firmware...")
        if self.firmware.dma_need_reinit():
            logger.info("WFDMA needs re-init; running light dma_init(resume).")
            self.firmware._dma_init(resume=True)
        # Drain any RX the prior session left buffered on EP 0x84 before re-posting the
        # reader: a fresh WinUSB handle otherwise reads that stale data first and shadows
        # the GET_NIC_CAPAB reply below (seq-mismatch -> the query times out).
        self.transport.drain_rx()
        self.transport.start_rx()

        # Warm skips post_boot_init, so mac_address would stay None and antenna_mask its 0x3
        # default. GET_NIC_CAPAB is a firmware query the already-running FW answers with the
        # silicon MAC + caps (HW-verified on a warm chip: returns 9c:ef:d5:.. + real caps),
        # needing no re-init, so read it here to fill both. Best-effort with a few retries
        # (a fresh handle can need a beat to settle): a failed query leaves the card usable,
        # only MAC-less.
        for _ in range(3):
            try:
                cmd, payload = mcu.get_nic_capability()
                resp = await self.transport.send_mcu_command(cmd, payload)
                caps = mcu.parse_nic_capability(resp or b"")
            except Exception as e:                   # noqa: BLE001
                logger.warning("MT7921AU warm reattach: GET_NIC_CAPAB failed (%s); "
                               "mac_address stays None.", e)
                break
            if caps.mac:
                self.mac_address = caps.mac
                self._refine_product_name()
                self._antenna_mask = caps.antenna_mask
                self._nic_has_6ghz = int(caps.has_6ghz)
                self._log_nic_caps(caps)
                break

        if progress_cb:
            progress_cb(0.9, "Tuning...")
        await self.set_channel(self._channel)
        self.is_warm = True
        if progress_cb:
            progress_cb(1.0, "Done")
        logger.info("MT7921AU warm reattach ready on channel %d.", self._channel)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 20 MHz channel via the monitor sniffer config command."""
        logger.debug("MT7921AU: tuning to channel %d", channel)
        cmd, payload = mcu.config_sniffer(channel)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the connac2 TX descriptor (HW ACK-retry limit 15, NO_ACK clear,
        byte-verified by verify_pcap CHECK 4 against the captured aireplay TX) and send it once
        on the frame's USB bulk-OUT endpoint: mgmt/ctrl on HCCA (0x09), data on AC_BE (0x04).
        The seq is already stamped (``_stamp_tx_seq``) and the hardware appends the FCS, so pass
        the bare MPDU. The TX rate is the current channel's band basic rate."""
        try:
            wire, endpoint = tx.build_tx(frame_bytes, band_5ghz=self._channel > 14)
        except ValueError as e:
            logger.error("MT7921AU inject_frame: %s", e)
            return False
        return await self.transport.send_bulk_checked(wire, endpoint)

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Software-stamp an incrementing 802.11 sequence number (frag-preserving, one step is
        0x10). build_tx sets the TXD SN_VALID with this seq, so the chip transmits exactly what
        we stamp; without it every inject reuses seq 0 and an AP dedups our multi-frame attacks
        (ChopChop, fragmentation, fake-auth) as retransmissions. See tx.stamp_seq_ctrl."""
        buf = bytearray(frame_bytes)
        self._tx_seqno = tx.stamp_seq_ctrl(buf, self._tx_seqno)
        return bytes(buf)

    async def _enable_rx_acks(self) -> None:
        """Clear RFCR DROP_UNWANTED_CTL via the FW MCU so monitor RX admits the AP's ACK control
        frames (FC=0xD4) to a MAC we inject as. A real register write (MCU call), not a no-op;
        the base arms the tally and clears the counts. Distinct from active monitor, which makes
        the chip EMIT ACKs."""
        await chip_init.admit_ack_frames(self.transport)

    async def _disable_rx_acks(self) -> None:
        """Restore the monitor default (re-set RFCR DROP_UNWANTED_CTL)."""
        await chip_init.drop_ack_frames(self.transport)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Arm HW auto-ACK for ``mac`` by programming it as the device omac (connac2
        ACKs on RA==omac); return the MAC armed. The monitor BSS is already active from
        bring-up, so this is DEV_INFO with a non-zero omac, plus the peer ``bssid`` into
        the BSS when given. The BSS goes in as CONNECTION_MONITOR (conn_type=0), NOT the
        bring-up INFRA_AP: the AP's frames are auto-ACKed only when the peer bssid is
        programmed, but INFRA_AP+bssid switches the firmware to a peer-STA context (no
        add_sta) that kills the omac auto-ACK entirely (mcu.CONNECTION_MONITOR).
        Reversed by exit_active_monitor."""
        cmd, payload = mcu.uni_dev_info(True, bytes(mac))
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        if bssid is not None:
            cmd, payload = mcu.uni_bss_info(True, bytes(bssid), conn_type=mcu.CONNECTION_MONITOR)
            await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the plain-monitor baseline (re-zero the omac + BSS bssid). The BSS
        stays active — its resting state since bring-up, where a zero omac matches
        nothing."""
        cmd, payload = mcu.uni_dev_info(True, b"\x00" * 6)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        cmd, payload = mcu.uni_bss_info(True, b"\x00" * 6)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)

    async def close(self):
        await self.transport.stop_rx()

    def _on_raw_rx(self, data: bytes):
        """Decode one 802.11 frame off EP 0x84 (MCU responses are demuxed away by
        the transport). Strips the connac2 RX descriptor, then parses the MPDU."""
        decoded = rx.decode_frame(data, self._antenna_mask)
        if decoded is None:
            return
        mpdu_off, mpdu_end, rssi, fcs_err = decoded
        if fcs_err:
            return
        # Slice to MT_RXD0_LENGTH, not the buffer end — the tail is alignment
        # padding; including it over-reads IEs (WEP->WPA2 flip) and breaks the
        # WEP/WPS/frag length math (see rx.decode_frame).
        frame_bytes = data[mpdu_off:mpdu_end]
        if len(frame_bytes) < 10:
            return
        # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
        # iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
        if len(frame_bytes) == 10 and frame_bytes[0] == 0xD4:
            self.record_ack(frame_bytes)
            return
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7921AU frame parse fail: {e}")
