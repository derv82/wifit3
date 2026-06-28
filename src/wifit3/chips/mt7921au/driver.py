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
from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)


class MT7921AUDriver:
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

    SUPPORTED_IDS = [
        DeviceID(0x0e8d, 0x7961, "Mediatek MT7921AU (ALFA AWUS036AXML)"),
    ]
    # Dual-band Wi-Fi 6 radio, 20 MHz primary. 2.4 GHz (1-13) + the 5 GHz 20 MHz
    # channels of the world regulatory domain (regdomain.CHANNELS_5GHZ).
    SUPPORTED_CHANNELS = list(range(1, 14)) + [
        36, 40, 44, 48, 52, 56, 60, 64,
        100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165,
    ]
    # Auto-ACK doesn't appear to work: one WPS PBC exchange logs ~120 EAPOL (vs ~15-25 when
    # it works), and mt7921u is reported to lack active monitor (openwrt/mt76#839, USB-WiFi#107).
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED

    # Device setup installs the udev rule + modprobe blocklist, but neither applies until the next
    # device-add — until then the kernel's mt7921u still owns the interface and our claim/control
    # transfers EACCES. Auto-connecting into that fails before the driver gets a clean shot. So gate
    # on a replug: after the rules land, the card re-enumerates cold (kernel blocklisted, udev rule
    # live) and our cold boot runs on a pristine chip. See MT7921AU.md. (Cold/normal plug unaffected.)
    LINUX_REPLUG_AFTER_MODPROBE = True

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7921AUDriver":
        return cls(dev)

    def __init__(self, dev):
        self.dev = dev
        self.transport = MT7921AUTransport(dev)
        self.firmware = MT7921AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._init_state: Optional[chip_init.InitState] = None
        self._channel = self.SUPPORTED_CHANNELS[0]
        # WlanDriver protocol runtime state. is_warm reflects the bring-up path taken
        # by connect(): True when we light-reattached to already-running firmware
        # (_warm_reattach), False on a cold boot. mac_address is parsed from the
        # GET_NIC_CAPAB reply during cold boot (MT_NIC_CAP_MAC_ADDR TLV); it stays None
        # on a warm reattach, which skips post-boot init.
        self.is_warm: bool = False
        self.mac_address: Optional[str] = None
        self._nic_has_6ghz: int = 0
        # 802.11 TX sequence counter (number in seq_ctrl bits [4:15], so it steps
        # by 0x10). The chip transmits the seq we stamp (TXD SN_VALID), so we own
        # it — see tx.stamp_seq_ctrl. Touched on the event loop only (no lock).
        self._tx_seqno: int = 0

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

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
        logger.info("MT7921AU warm-check: MT_CONN_ON_MISC=0x%x", misc)
        return (misc & MT_TOP_MISC2_FW_N9_RDY) != 0

    async def _cold_boot(self, progress_cb: Optional[ProgressCallback]) -> bool:
        """Full bring-up of a cold chip: firmware upload, post-boot device init,
        monitor entry. The RX reader is started by load_firmware."""
        if progress_cb:
            progress_cb(0.1, "Uploading firmware...")
        if not await self.firmware.load_firmware():
            logger.error("Failed to load MT7921AU firmware.")
            return False
        self.transport.start_rx()   # idempotent — load_firmware already started it

        if progress_cb:
            progress_cb(0.6, "Configuring device...")
        logger.info("Running MT7921AU post-boot init...")
        self._init_state = await chip_init.post_boot_init(self.transport)
        caps = mcu.parse_nic_capability(self._init_state.nic_capab_resp)
        self.mac_address = caps["mac"]
        self._nic_has_6ghz = caps["has_6ghz"]
        logger.info("MT7921AU silicon MAC: %s", self.mac_address)

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
        self.transport.start_rx()
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

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit a raw 802.11 frame.

        Builds the connac2 TX descriptor (tx.build_tx, byte-verified by verify_pcap
        CHECK 4 against the captured aireplay TX) and sends it on the frame's USB
        bulk-OUT endpoint — mgmt/ctrl on HCCA (0x09), data on AC_BE (0x04). The TX
        rate is the current channel's band basic rate. The hardware appends the FCS,
        so pass the bare MPDU.
        """
        # Stamp an incrementing sequence number (frag-preserving) before building
        # the descriptor — the chip sends whatever seq we provide, so without this
        # every injected frame reuses seq 0 and the AP dedups interactive attacks.
        buf = bytearray(frame_bytes)
        self._tx_seqno = tx.stamp_seq_ctrl(buf, self._tx_seqno)
        try:
            wire, endpoint = tx.build_tx(bytes(buf), band_5ghz=self._channel > 14,
                                         no_ack=use_no_ack)
        except ValueError as e:
            logger.error("MT7921AU inject_frame: %s", e)
            return False
        return await self.transport.send_bulk_checked(wire, endpoint)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Arm HW auto-ACK for ``mac`` by programming it as the device omac (connac2
        ACKs on RA==omac); return the MAC armed. The monitor BSS is already active from
        bring-up, so this is DEV_INFO with a non-zero omac, plus the peer ``bssid`` into
        the BSS when given. Reversed by exit_active_monitor."""
        cmd, payload = mcu.uni_dev_info(True, bytes(mac))
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        if bssid is not None:
            cmd, payload = mcu.uni_bss_info(True, bytes(bssid))
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
        decoded = rx.decode_frame(data)
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
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7921AU frame parse fail: {e}")
