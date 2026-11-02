"""Userspace driver for the MediaTek MT7601U (2.4 GHz USB Wi-Fi dongle).

Wires the ported stages into the Driver contract: EEPROM decode and MAC programming,
firmware download, post-firmware hardware init, the channel tune, and the RX descriptor
decode feeding the frame parser.

Transmission is ported. Injection writes a real TX descriptor on the verified path and is
byte-identical to the kernel's own over 7680 recorded de-auths (capture-6). Receiving works on
hardware: the RX URBs are submitted between mcu_cmd_init and write_mac_initvals, where
dma.c:517 mt7601u_dma_init sits -- armed earlier the chip never streams a byte.

State of the port (see docs/porting/METHODOLOGY.md for the gates each stage passed):

  EEPROM read + decode     verified 160/160 ops on two cold-boot captures
  firmware download        verified 78 and 77 ops on the same two captures
  station memory clears   verified 13/13 burst ops on two cold-boot captures
  hardware init            ported, register-for-register equivalent to the kernel
  channel tune             verified 55/55 recorded tunes in monitor mode
  RX descriptor decode     unit-tested, and delivering real frames on hardware
  TX descriptors           byte-identical to the kernel over 7680 recorded de-auths
  transmit queue           ported; USB-level completion verified, air unconfirmed
  association / keys / AP  not ported -- blocks PMKID, WPS, EvilTwin
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.chips.mt7601u.constants import (
    HZ,
    MT_ASIC_VERSION,
    MT_CALIBRATE_INTERVAL,
    MT_EFUSE_CTRL,
    MT_EFUSE_CTRL_SEL,
    MT_MAC_ADDR_DW0,
    MT_MAC_ADDR_DW1,
    MT_MAC_ADDR_DW1_U2ME_MASK,
    MT_MAC_CSR0,
    MT_USB_DMA_CFG,
)
from wifit3.chips.mt7601u.eeprom import MT7601UEeprom, MT7601UEepromParams
from wifit3.chips.mt7601u.firmware import find_firmware, load_firmware
from wifit3.chips.mt7601u.init import MT7601UInit
from wifit3.chips.mt7601u.mac import STAT_WORK_INTERVAL_S, MacStats, mac_work
from wifit3.chips.mt7601u.mcu import MT7601UMcu, McuTimeout
from wifit3.chips.mt7601u.phy import MT7601UPhy
from wifit3.chips.mt7601u.rx import iter_frames
from wifit3.chips.mt7601u.transport import MT7601UTransport
from wifit3.chips.mt7601u.tx import TX_QUEUE_INJECT as DEFAULT_TX_QUEUE
from wifit3.chips.mt7601u.tx import stamp_seq_ctrl
from wifit3.chips.mt7601u.cal import phy_calibrate
from wifit3.chips.mt7601u.tx_status import TxStatus
from wifit3.chips.mt7601u.tx_ring import TxQueues
from wifit3.chips.mt7601u.wcid import init_station_memory
from wifit3.chips.rx_reader import RxReaderThread
from wifit3.dot11.parser import WlanFrameParser
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

logger = logging.getLogger(__name__)

RX_BUFFER_SIZE = 32768
"""mt7601u.h MT_RX_ORDER 3, so the RX URBs are 4 pages: 16 * 32768 = 512 KiB."""

CALIBRATE_INTERVAL_S = MT_CALIBRATE_INTERVAL / HZ
"""mt7601u.h:22 MT_CALIBRATE_INTERVAL is 4 * HZ jiffies; asyncio.sleep wants seconds."""

class MT7601UDriver(Driver):
    """Receive-only userspace driver for the MT7601U."""

    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 15))
    """2.4 GHz only. Channel 14 is tuned from the port's own table, bypassing the
    nl80211 regdomain check that refuses it under most domains; the kernel driver never
    sees that error, so the port must not invent one."""

    CONFLICTING_LINUX_MODULES: ClassVar[List[str]] = ["mt7601u"]
    """The in-kernel driver claims 17 of the IDs in SUPPORTED_IDS, so Linux setup must
    blocklist it or the kernel owns the interface before we can claim it."""

    LINUX_REPLUG_AFTER_MODPROBE = True

    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.SPOOFABLE
    """mac.c:22-24 -- MT_MAC_ADDR_DW0/DW1 are plain writable registers on this silicon, so the
    autoresponder will HW-ACK whatever MAC is programmed there. Same silicon path as mt76x0u."""

    MAX_ACK_DELAY: ClassVar[float] = 0.25
    """Inherited default 0.02 is wrong for this chip. Measured on two dongles, ch6, armed
    autoresponder on A and injecting from B, polling the tally every 1ms: min 2.1ms, median
    57ms, p90 73ms, max 120ms, with 14 of 15 trials landing outside the 20ms window. The
    round-trip is dominated by this chip's USB bulk-RX scheduling, not by a register:
    clearing MT_USB_DMA_CFG_RX_BULK_AGG_EN moved the median only 60.1ms -> 61.2ms, so there
    is no aggregation knob to turn. At 0.02 every genuinely ACKed frame reported False, which
    is what made ACK-measured deauth look broken. 0.25 clears the measured max with margin for
    a busy channel and is still short enough that a retry loop does not visibly stall."""

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "MT7601UDriver":
        drv = cls(dev)
        drv.product_name = id_entry.product_name
        return drv

    def __init__(self, dev: usb.core.Device) -> None:
        super().__init__()
        self.dev = dev
        self.transport = MT7601UTransport(dev)
        self.mcu = MT7601UMcu(self.transport)
        self.ee: MT7601UEepromParams = MT7601UEepromParams()
        # The reader fills this same object in place, so phy and the driver stay on
        # one EEPROM record; passing a separate one silently starved the TX power path.
        self.eeprom_dev = MT7601UEeprom(self.transport, self.ee)
        self.phy = MT7601UPhy(self.transport, self.mcu, self.ee)
        self.chip_init = MT7601UInit(self.transport, self.mcu, self.phy)
        self.parser = WlanFrameParser()
        self.is_warm: bool = False
        self.mac_address: Optional[str] = None
        self._channel: int = self.SUPPORTED_CHANNELS[0]
        self._tx_seqno: int = 0
        self._cal_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self.stats = MacStats()
        self._rx_callback: Optional[Callable] = None
        self._disconnect_callback: Optional[Callable] = None
        self._reader: Optional[RxReaderThread] = None
        # One pool per OUT endpoint; injection goes to the best-effort queue.
        self._tx_queues = TxQueues(self.transport, self.mcu)

    # ---- callbacks ----------------------------------------------------

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable) -> None:
        self._disconnect_callback = cb

    # ---- bring-up -----------------------------------------------------

    def _refuse_kernel_bound_chip(self) -> None:
        """Refuse a dongle the kernel driver has already probed.

        ``transport.claim`` silently detaches a bound kernel driver, so nothing downstream
        distinguishes the two cases. They are not equivalent: mt7601u initialises the chip
        during probe, and that init is not undone by unloading, so a bring-up onto such a
        chip completes normally and RX then returns zero bytes at every read timeout, with
        no error anywhere to explain it. Detaching hides the diagnosis and strands the user
        watching an empty scan. Refuse while the interface is still bound, when the cause is
        still knowable.
        """
        try:
            bound = self.transport.dev.is_kernel_driver_active(0)
        except (NotImplementedError, usb.core.USBError):
            return          # libusb only answers this on Linux; elsewhere there is no module
        if not bound:
            return
        raise BringUpError(
            "MT7601U: the kernel mt7601u driver is bound to this dongle. It initialises the "
            "chip when it probes, and this bring-up cannot undo that init, so RX would stay "
            "silent. Blacklist the module and replug: "
            "echo 'blacklist mt7601u' | sudo tee /etc/modprobe.d/blacklist-mt7601u.conf, "
            "then unplug and re-insert the dongle. Unloading it afterwards is not enough -- "
            "the init already happened."
        )

    def _require_live_chip(self) -> None:
        """Fail loudly unless the chip answers a register read.

        ``rr`` reports a failed or short transfer as ~0, and ``vendor_request``
        swallows the timeout after its retries, so a card whose control endpoint has
        stopped responding reads back as ~0 for every register. Without this gate the
        EEPROM decodes to a garbage MAC, bring-up writes that garbage into the chip and
        connect() returns True -- presenting a dead card as a healthy one.
        """
        probe = self.transport.rr(MT_USB_DMA_CFG)
        if probe == 0xFFFFFFFF:
            raise BringUpError(
                f"MT7601U: no response from {MT_USB_DMA_CFG:#06x}. The control endpoint is "
                "not answering, so this card cannot be brought up. Unplug and replug it."
            )

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Host-side attach -- kernel-driver refusal, interface claim, pipe map -- plus wifit3's
        own liveness probe, then the ported bring-up. The probe reads a register the kernel
        never touches here, so it stays outside _bringup()."""
        self._refuse_kernel_bound_chip()
        self.transport.claim()
        self.transport.assign_pipes()
        self._require_live_chip()
        return await self._bringup(progress_cb)

    async def _bringup(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """init.c:318 mt7601u_init_hardware, including its error path.

        init.c:413-423 unwinds through err_rx -> err_mcu -> err on every failure. Only
        chip_onoff(false) reaches the wire -- dma_cleanup and mcu_cmd_deinit are host-side
        URB teardown -- and without it a bring-up that fails midway leaves the chip powered
        with WLAN_EN set and DMA armed.
        """
        try:
            return await self._init_hardware(progress_cb)
        except BaseException:
            if self._reader is not None:                        # init.c:414 dma_cleanup
                await self._reader.stop()
                self._reader = None
            try:
                self.chip_init.chip_onoff(False)                # init.c:422
            except Exception as exc:
                logger.warning("MT7601U: power-down after a failed bring-up failed: %s", exc)
            raise

    async def _init_hardware(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """The kernel's cold-boot register sequence, in its order (init.c:318
        mt7601u_init_hardware). verify_pcap replays _bringup against this."""
        def step(fraction: float, message: str) -> None:
            if progress_cb is not None:
                progress_cb(fraction, message)


        # usb.c:289-306: the ASIC must answer before anything is programmed, and the revision
        # gate is the kernel's own discriminator against the mt76x0u sharing VID:PID 148f:760a.
        step(0.10, "Probing ASIC")
        self.chip_init.wait_asic_ready()
        asic_rev = self.transport.rr(MT_ASIC_VERSION)
        mac_rev = self.transport.rr(MT_MAC_CSR0)
        logger.info("MT7601U: ASIC revision %08x MAC revision %08x", asic_rev, mac_rev)
        if (asic_rev >> 16) != 0x7601:                                      # usb.c:299
            raise BringUpError(
                "asic_id", f"ASIC revision {asic_rev:#010x} is not an MT7601U")
        if not self.transport.rr(MT_EFUSE_CTRL) & MT_EFUSE_CTRL_SEL:        # usb.c:305
            logger.warning("MT7601U: eFUSE not present")

        # init.c:330-347 gates the WLAN clock, waits for the ASIC, then downloads the
        # firmware, polls WPDMA idle, and waits for the ASIC a second time. Pushing the
        # image while WLAN_EN is still clear leaves the RF path ungated, and the port
        # ran it in the opposite order.
        step(0.35, "Powering up chip")
        self.chip_init.chip_onoff(True)
        self.chip_init.wait_asic_ready()

        step(0.60, "Downloading firmware")
        # mcu.c:416 firmware_running is the chip's own cold-vs-warm test: MT_MCU_COM_REG0
        # reads 1 when a previous session left the MCU up and the download is skipped.
        self.is_warm = load_firmware(self.transport, self.mcu, find_firmware())
        self.chip_init.poll_dma_idle(100)                 # init.c:339 mt76_poll_msec
        self.chip_init.wait_asic_ready()
        self.chip_init.reset_csr_bbp()
        self.chip_init.init_usb_dma()
        self.chip_init.mcu_cmd_init()
        # dma.c:517 mt7601u_dma_init submits the RX URBs here. The chip only streams
        # frames once they are armed after mcu_cmd_init; armed earlier it stays silent.
        self._start_rx()
        self.chip_init.write_mac_initvals()
        self.chip_init.poll_mac_idle()
        self.chip_init.init_bbp()
        # init.c runs the station-memory clears after the BBP tables.
        # A TX descriptor carries a wcid that must resolve to a valid slot.
        init_station_memory(self.mcu)
        self.chip_init.pre_phy_finalise()

        # init.c:396 reads the EEPROM here, immediately before phy_init: the power tables it
        # fills are what phy_init programs. read() fills its own self.ee in place and must not
        # be rebound -- phy was constructed with this object, and a rebind left the TX power
        # path reading an empty table, so every MT_TX_PWR_CFG_* register went to zero and the
        # chip transmitted at zero power.
        step(0.80, "Reading EEPROM")
        self.eeprom_dev.read()
        mac = self.eeprom_dev.macaddr
        self.mac_address = ":".join(f"{b:02x}" for b in mac) if any(mac) else None

        self.phy.phy_init()
        self.chip_init.finalise()
        self.chip_init.mac_start()

        step(0.90, f"Tuning to channel {self._channel}")
        self.phy.set_channel(self._channel)

        self._tx_queues = TxQueues(self.transport, self.mcu)
        # main.c:22-25 queues both delayed works once the MAC is started. cal_work keeps
        # phy.raw_temp moving, without which temp_comp's DPD and PLL-protect branches can
        # never fire again; mac_work sweeps the read-to-clear counters and is the only
        # thing that calls check_mac_err.
        self._cal_task = asyncio.create_task(self._periodic(
            CALIBRATE_INTERVAL_S, lambda: phy_calibrate(self.phy), "calibration"))
        self._stats_task = asyncio.create_task(self._periodic(
            STAT_WORK_INTERVAL_S, lambda: mac_work(self.transport, self.stats), "stats"))
        step(1.0, "Ready")
        return True

    async def _periodic(self, interval: float, work: Callable[[], None],
                        label: str) -> None:
        """One of the kernel's delayed works, re-queued as phy.c:1014 and mac.c:351 do.

        A USB hiccup on one tick is not worth tearing the interface down for, so a failed
        tick is logged and the next one still runs.
        """
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    work()
                except (IOError, usb.core.USBError, McuTimeout) as exc:
                    logger.debug("MT7601U: %s tick skipped: %s", label, exc)
        except asyncio.CancelledError:
            pass

    def _start_rx(self) -> None:
        loop = asyncio.get_event_loop()
        self._reader = RxReaderThread(
            loop,
            self._read_once,
            self._dispatch,
            name="mt7601u-rx",
            on_fatal=self._disconnect_callback,
        )
        self._reader.start()

    def _read_once(self) -> Optional[bytes]:
        return self.transport.bulk_in_rx(RX_BUFFER_SIZE) or None

    def _dispatch(self, buffer: bytes) -> None:
        if self._rx_callback is None and not self._ack_detect_on:
            return
        offset = self.ee.rssi_offset[0] if self.ee.rssi_offset else 0
        for frame in iter_frames(buffer, self.ee.lna_gain, offset):
            raw = frame.frame
            # A 10-byte 0xD4 frame is an ACK. The parser drops control frames, so it has
            # to be tapped here on the raw bytes; record_ack itself no-ops unless the
            # tally is armed and RA matches a MAC we injected as.
            if len(raw) == 10 and raw[0] == 0xD4:
                self.record_ack(raw)
                continue
            if self._rx_callback is None:
                continue
            packet = self.parser.parse_80211_frame(raw, frame.rssi)
            if packet is not None:
                self._rx_callback(packet)

    # ---- channel ------------------------------------------------------

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel``. ``scan=True`` is the kernel's MT7601U_STATE_SCANNING.

        main.c:271 and :281 bracket a scan with agc_save/agc_restore, and the flag's
        transition is the only signal the Driver ABC carries for those two hooks.
        """
        if channel not in self.SUPPORTED_CHANNELS:
            return False
        if scan:
            self.phy.agc_save()
        else:
            self.phy.agc_restore()
        self.phy.set_channel(channel, scan=scan)
        self._channel = channel
        return True

    # ---- teardown -----------------------------------------------------

    async def close(self) -> None:
        """main.c:31 mt7601u_stop then init.c:422 mt7601u_cleanup, in the kernel's order.

        The MAC stops before the reader does: init.c:285-296 drains the RX queue, which only
        empties while the host is still consuming bulk-IN.
        """
        for name in ("_cal_task", "_stats_task"):    # main.c:37-38
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)
        try:
            self.chip_init.mac_stop_hw()             # init.c:305 mt7601u_mac_stop
        except Exception as exc:                     # teardown must not mask the real error
            logger.warning("MT7601U: MAC stop failed: %s", exc)
        if self._reader is not None:                 # dma.c:541 mt7601u_dma_cleanup
            await self._reader.stop()
            self._reader = None
        if self._tx_queues is not None:
            self._tx_queues.close()
        try:
            self.chip_init.chip_onoff(False)         # init.c:312 mt7601u_stop_hardware
        except Exception as exc:
            logger.warning("MT7601U: WLAN shutdown failed: %s", exc)
        self.transport.release()
        self.transport.dispose()

    # ---- TX ------------------------------------------------------------

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Send one frame. False means the chip did not accept it.

        Requests the link-layer ACK only for a unicast addr1 (tx.c:158), so the MAC retransmits
        until the peer ACKs; a group addr1 goes out once.
        """
        return self._tx_queues[DEFAULT_TX_QUEUE].submit(frame_bytes, ack=not frame_bytes[4] & 0x01)

    def tx_statuses(self) -> list[TxStatus]:
        """Per-frame transmit results the MAC has reported since the last reset.

        The kernel hands these to mac80211 as they arrive; here they are drained
        on every submit and kept on the queue. ``SUCCESS`` means the silicon
        completed a transmission, not that a frame left the antenna, so it is a
        floor on what was sent and never proof it was heard.
        """
        if self._tx_queues is None:
            return []
        return list(self._tx_queues[DEFAULT_TX_QUEUE].statuses)

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Software-stamp an incrementing 802.11 sequence number into seq_ctrl.

        MT_TXWI_ACK_CTL_NSEQ is the bit that asks the MAC to assign the number
        (rt2800.h:3097), tx.c:160 sets it only for ASSIGN_SEQ, and the kernel's monitor
        descriptor leaves it clear -- so the MPDU's own seq_ctrl is what goes on the air.
        """
        buf = bytearray(frame_bytes)
        self._tx_seqno = stamp_seq_ctrl(buf, self._tx_seqno)
        return bytes(buf)

    async def enter_active_monitor(self, mac: bytes,
                                   bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` into MT_MAC_ADDR_DW0/DW1 so the autoresponder HW-ACKs it (mac.c:22-24).

        U2ME stays 0xff throughout: the kernel arms it in mt7601u_set_macaddr, which is the
        only place this silicon ever gets it, so the monitor baseline already has it set and
        exit must not diverge. ``bssid`` is unused -- unlike a firmware-offload radio, this
        autoresponder answers on DW0 alone.
        """
        self._write_self_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the EEPROM MAC, keeping U2ME as mac.c set it."""
        if not self.mac_address:
            return
        self._write_self_mac(bytes.fromhex(self.mac_address.replace(":", "")))

    def _write_self_mac(self, mac: bytes) -> None:
        """mac.c:22-24 -- DW0 takes the low 4 bytes, DW1 the high 2 plus the U2ME mask.

        Synchronous register writes, like every other MT7601U register path in this driver.
        """
        self.transport.wr(MT_MAC_ADDR_DW0, int.from_bytes(mac[0:4], "little"))
        self.transport.wr(MT_MAC_ADDR_DW1,
                          int.from_bytes(mac[4:6], "little") | MT_MAC_ADDR_DW1_U2ME_MASK)

    async def _enable_rx_acks(self) -> None:
        """No-op: the monitor RX filter is promiscuous, so ACK/CTS/RTS already reach the
        RX stream and _dispatch taps ACKs off the raw bytes."""

    async def _disable_rx_acks(self) -> None:
        """No-op, matching _enable_rx_acks."""
