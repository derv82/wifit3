import logging
import struct
from pathlib import Path
from typing import Callable, Optional

import usb.core

from . import init as chip_init
from . import mcu, rx, tx
from .transport import MT7925AUTransport
from .firmware import MT7925AUFirmwareLoader
# ruff: noqa: F403, F405
from .constants import *
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError

logger = logging.getLogger(__name__)


class MT7925AUDriver(Driver):
    """Userspace driver for the MediaTek MT7925U (Wi-Fi 7, connac3, USB).

    Bring-up state (see chips/mt7925au/MT7925AU.md): firmware boot (firmware.py) is
    ported and pcap-verified. Post-boot device init, monitor entry, channel tune,
    RX decode and TX are in progress.
    """

    # Dual-band Wi-Fi 7 radio, 20 MHz primary. 2.4 GHz (1-14) + the 5 GHz 20 MHz
    # channels the capture sweeps (main.log: 36..165).
    SUPPORTED_CHANNELS = list(range(1, 15)) + [
        36, 40, 44, 48, 149, 153, 157, 161, 165,
    ]
    FAKE_MAC = FakeMacSupport.SPOOFABLE
    LINUX_REPLUG_AFTER_MODPROBE = True

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7925AUDriver":
        drv = cls(dev)
        drv.product_name = id_entry.product_name
        return drv

    def __init__(self, dev):
        super().__init__()
        self.dev = dev
        self.transport = MT7925AUTransport(dev)
        self.firmware = MT7925AUFirmwareLoader(self.transport, Path(__file__).parent / "assets")
        self.parser = WlanFrameParser()
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._channel = self.SUPPORTED_CHANNELS[0]
        self.is_warm: bool = False
        self.mac_address: Optional[str] = None
        self._antenna_mask: int = 0x3
        self._tx_seq: int = 0
        # The monitor link's TX wcid (mt7925/main.c:390: MT792x_WTBL_RESERVED - vif idx 0).
        self._tx_wcid: int = MT792x_WTBL_RESERVED

    def register_rx_callback(self, callback: Callable[[dict], None]):
        self._rx_callback = callback

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        self.transport._on_fatal = cb

    def _detect_warm(self) -> bool:
        """True if firmware is already running (FW_N9_RDY set) from a prior session. Claims
        the vendor interface for register access but does NOT clear-halt or reset — a warm
        chip must not be cold-booted (mcu_power_on would poison its pipes on WinUSB)."""
        self.firmware._claim_vendor_interface(clear_halts=False)
        try:
            misc = self.transport.read_reg32(MT_CONN_ON_MISC)
        except usb.core.USBError:
            return False
        return (misc & MT_TOP_MISC2_FW_N9_RDY) != 0

    async def _warm_reattach(self, progress_cb: Optional[ProgressCallback]) -> bool:
        """Light reattach to firmware already running in monitor mode (kernel mt7921u_resume
        model): no reset, no mcu_power_on, no reload, no post-boot init. Re-establish only
        the host interface:

          - a light dma_init(resume) IFF the WFDMA NEED_REINIT latch was cleared
            (mt792x_dma_need_reinit); a normal reconnect leaves it set, so skipped.
          - drain any RX the prior session left buffered on EP 0x84 (a fresh WinUSB handle
            otherwise reads that stale data first and shadows the MCU reply).
          - re-post RX (== mt76u_resume_rx) and re-read the caps.

        GET_NIC_CAPAB is best-effort with a few retries: a fresh WinUSB handle can need a
        beat to settle, and a failed query leaves the card usable (only MAC-less), never a
        hard replug. This mirrors the mt7921au sibling on the same mt792x stack."""
        logger.info("MT7925AU warm reattach (firmware already running)...")
        if progress_cb:
            progress_cb(0.5, "Reattaching to running firmware...")
        if self.firmware.dma_need_reinit():
            logger.info("WFDMA needs re-init; running light dma_init(resume).")
            self.firmware._dma_init(resume=True)
        self.transport.drain_rx()
        self.transport.start_rx()

        caps = mcu.NicCaps()
        for _ in range(3):
            resp = await self.transport.send_mcu_command(*mcu.get_nic_capability())
            caps = mcu.parse_nic_capability(resp or b"")
            if caps.mac:
                break
        if caps.mac:
            self.mac_address = caps.mac
            self._antenna_mask = caps.antenna_mask
        else:
            logger.warning("MT7925AU warm reattach: GET_NIC_CAPAB unanswered after retries; "
                           "continuing MAC-less (the card is still usable).")
        await self.set_channel(self._channel)
        self.is_warm = True
        if progress_cb:
            progress_cb(1.0, "Done")
        logger.info("MT7925AU warm reattach ready on channel %d (MAC %s).",
                    self._channel, self.mac_address)
        return True

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold-boot the firmware and enter monitor mode, or light-reattach if firmware is
        already running (warm)."""
        self.transport.subscribe(self._on_raw_rx)
        if self._detect_warm():
            return await self._warm_reattach(progress_cb)

        if progress_cb:
            progress_cb(0.1, "Uploading MT7925AU firmware...")
        if not await self.firmware.load_firmware():
            raise BringUpError("firmware", "MT7925AU firmware load failed")
        self.transport.start_rx()

        if progress_cb:
            progress_cb(0.6, "Configuring device...")
        state = await chip_init.post_boot_init(self.transport)
        self.mac_address = state.caps.mac
        self._antenna_mask = state.caps.antenna_mask

        if progress_cb:
            progress_cb(0.9, "Enabling monitor mode...")
        await chip_init.enter_monitor(self.transport, self._channel, state.caps.has_6ghz)
        if progress_cb:
            progress_cb(1.0, "Done")
        logger.info("MT7925AU monitor mode ready on channel %d (MAC %s).",
                    self._channel, self.mac_address)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 20 MHz channel via the monitor sniffer config (UNI SNIFFER, tag 1)."""
        cmd, payload = mcu.config_sniffer(channel)
        await self.transport.send_mcu_command(cmd, payload, wait_resp=False)
        self._channel = channel
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the connac3 TXWI for a raw 802.11 frame and send it once over the HCCA
        bulk-OUT (EP 0x09), the mgmt/PSD endpoint the mt76 xmit path routes injected
        frames to. The TXWI reads the 802.11 sequence from the frame itself."""
        wire = tx.build_tx(frame_bytes, wcid_idx=self._tx_wcid)
        return await self.transport.send_bulk_checked(wire, EP_OUT_HCCA)

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Stamp an incrementing 12-bit 802.11 sequence into seq_ctrl (bytes 22-23),
        preserving the 4-bit fragment field. build_tx copies it into txwi[3] SEQ."""
        if len(frame_bytes) < 24:
            return frame_bytes
        self._tx_seq = (self._tx_seq + 1) & 0xFFF
        buf = bytearray(frame_bytes)
        frag = struct.unpack_from("<H", buf, 22)[0] & 0x000F
        struct.pack_into("<H", buf, 22, (self._tx_seq << 4) | frag)
        return bytes(buf)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Arm HW auto-ACK for ``mac`` by programming it as the device omac (connac
        auto-ACKs frames whose RA == omac); return the MAC armed. The monitor BSS is
        already active from bring-up, so this is DEV_INFO with a non-zero omac, plus the
        peer ``bssid`` into the BSS when given. That BSS goes in as CONNECTION_MONITOR
        (conn_type=0), not the bring-up INFRA_AP: under INFRA_AP a peer bssid switches the
        firmware to a peer-STA context that kills the omac auto-ACK. Reversed by
        exit_active_monitor. Same mt792x DEV_INFO/BSS_INFO mechanism as mt7921au."""
        await self.transport.send_mcu_command(*mcu.uni_dev_info(True, bytes(mac)),
                                              wait_resp=False)
        if bssid is not None:
            await self.transport.send_mcu_command(
                *mcu.uni_bss_info(True, bytes(bssid), conn_type=CONNECTION_MONITOR),
                wait_resp=False)
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the plain-monitor baseline (re-zero the omac + BSS bssid). The BSS stays
        active: its resting state since bring-up, where a zero omac matches nothing."""
        await self.transport.send_mcu_command(*mcu.uni_dev_info(True, b"\x00" * 6),
                                              wait_resp=False)
        await self.transport.send_mcu_command(*mcu.uni_bss_info(True, b"\x00" * 6),
                                              wait_resp=False)

    async def _enable_rx_acks(self) -> None:
        return None

    async def _disable_rx_acks(self) -> None:
        return None

    async def close(self):
        await self.transport.stop_rx()

    def _on_raw_rx(self, data: bytes):
        """Decode one 802.11 frame off EP 0x84 (MCU responses are demuxed by the
        transport). Full connac3 RX decode lands with M4."""
        decoded = rx.decode_frame(data, self._antenna_mask)
        if decoded is None:
            return
        mpdu_off, mpdu_end, rssi, fcs_err = decoded
        if fcs_err:
            return
        frame_bytes = data[mpdu_off:mpdu_end]
        if len(frame_bytes) < 10:
            return
        try:
            parsed = self.parser.parse_80211_frame(frame_bytes, rssi)
            if parsed and self._rx_callback:
                self._rx_callback(parsed)
        except Exception as e:
            logger.debug(f"MT7925AU frame parse fail: {e}")
