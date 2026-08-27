"""RTL8821CU / 1T1R 802.11ac — vendor (HALMAC/PHYDM) cleanroom DKMS port.

The byte-for-byte gate (``scripts/chips/rtl8821cu_dkms/verify_pcap.py``) drives this driver's public
interface — ``connect`` (cold init + airmon monitor entry), ``set_channel`` (the airodump hops)
and ``inject_frame`` (the aireplay-ng test + deauth TX) — and reproduces the **entire** cold-boot
capture (all 21409 ctrl + bulk-OUT ops) byte-for-byte, so what the gate verifies is exactly the
product code path. The chip→host interrupt-IN (C2H) and bulk-IN (RX) streams are a separate blind
spot the host-side replay does not model — see RTL8821CU_DKMS.md.

Registered in ``wlan/discovery.py``. ``connect`` claims the combo card's WiFi (vendor-class)
interface, starts the bulk-IN ``RxReaderThread``, runs ``bringup.cold_bringup`` (FW download +
MAC/BB/RF + BT-coex + the ch1 monitor tune over the ep-0x05 FW/TX pipe — which leaves the chip in
the vendor's exact receiving config, byte-for-byte), then runs the phydm watchdog on a background
task. ``set_channel`` and ``inject_frame`` drive the phydm tune and the TX-descriptor path. The
whole cold-boot pcap is reproduced byte-for-byte by ``scripts/chips/rtl8821cu_dkms/verify_pcap.py``, and
cold init is HW-validated (FW boots). Hardware status — including the open monitor-RX demod fault
(frames received, ~99% fail CRC) — is tracked in RTL8821CU_DKMS.md. Warm reattach and the ZeroCD
mode-switch discovery blocker are open. Shares no code with the other Realtek drivers (anti-DRY).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.chips.rx_reader import RxReaderThread
from wifit3.chips.driver import DeviceID, Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.dot11.parser import WlanFrameParser

from . import bringup, chan, efuse, mac, phy, tx, watchdog
from .rx import iter_frames
from .transport import Rtl8821cuTransport

logger = logging.getLogger(__name__)

CHANNELS_2G = list(range(1, 15))
# Non-DFS 5 GHz only for now; the capture also tunes DFS 52..144 but set_channel
# (and the DFS tune path) is a later milestone — see RTL8821CU_DKMS.md.
CHANNELS_5G = [36, 40, 44, 48, 149, 153, 157, 161, 165]

# Monitor-mode management-inject TX-descriptor attributes. [WIRE] every aireplay-ng frame in the
# capture (probe-req / RTS / auth / deauth) shares macid 1, QSEL_MGNT, raid 1, 1M CCK, retry off;
# only TXPKTSIZE + BMC (from addr1) + the XOR checksum vary, all derived from the 802.11 frame.
_QSEL_MGNT = 0x12              # [SRC] halmac_type.h HALMAC_TXDESC_QSEL_MGNT
_RAID_INJECT = 1              # [WIRE] aireplay tx-desc dw1[20:16]

_WIFI_INTF_CLASS = 0xFF        # combo card: the WiFi function is the vendor-specific interface
                               # (class 0xFF, #2); the Bluetooth interfaces 0/1 are class 0xE0

_WATCHDOG_PERIOD_S = 2.0        # phydm dynamic-check cadence [SRC] rtw_cmd.c rtw_dynamic_chk_wk

# 2.4 GHz re-lock after cold init: the cold ch1 tune lands before the antenna switches to WiFi, so
# the LO never locks (RF18 BIT16 stuck). _prime_2g_band re-runs the band switch post-antenna-switch.
_PRIME_2G_CH = 1

# The pcap-gated reference card's EFUSE burn. A card whose burn differs selects ported-but-hardware-
# untested branches (walker rows / cut / rf_set / rfe-2 / ant port), so connect() tags it once.
_REF_RFE_TYPE = 0x22            # rfe_type_expand (raw 0xCA): BTG, 1-Ant@main, combo
_REF_CUT = 4                    # hal chip_ver / dm cut_version


class Rtl8821cuDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.SPOOFABLE

    def __init__(self, dev: usb.core.Device):
        super().__init__()          # base owns the ACK tally (_ack_detect_on / _our_tx_macs / _ack_counts)
        self.dev = dev
        # FW download + TX bulk-OUT is on ep 0x05, not the transport's 0x04 default [WIRE]
        # coverage audit. (The offline gate's ReplayDevice ignores the endpoint, so this only
        # matters on real silicon — without it the FW download writes to the wrong pipe.)
        self.transport = Rtl8821cuTransport(dev, bulk_out_ep=0x05)
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.info = None                # EfuseInfo from cold_bringup; set_channel/inject need it
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._wifi_intf: Optional[int] = None       # claimed vendor (WiFi) interface number
        self._io_lock = asyncio.Lock()              # serialize set_channel / inject / watchdog / mac-write
        self._wd_state = None
        self._watchdog_task = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821cuDkmsDriver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    def _claim(self) -> None:
        """Combo card: set the configuration and claim the vendor-specific (class 0xFF) WiFi
        interface — NOT the Bluetooth interfaces (class 0xE0). The manager is chipset-agnostic and
        does not claim, so the driver must (mirrors test_hw's manual claim). No-op once claimed."""
        if self._wifi_intf is not None:
            return
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            logger.debug("set_configuration: %s", e)
        intf_num = next((i.bInterfaceNumber for i in self.dev.get_active_configuration()
                         if i.bInterfaceClass == _WIFI_INTF_CLASS), None)
        if intf_num is None:
            raise BringUpError("RTL8821CU: no vendor-specific WiFi interface — the combo card is "
                               "likely still in ZeroCD (CD-ROM) mode; mode-switch it first.")
        try:
            if self.dev.is_kernel_driver_active(intf_num):
                self.dev.detach_kernel_driver(intf_num)
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        usb.util.claim_interface(self.dev, intf_num)
        self._wifi_intf = intf_num

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """Cold bring-up + airmon monitor entry (``bringup.cold_bringup``), caching the decoded
        EFUSE/board info that ``set_channel`` and the watchdog/coex producers key on. The cold path
        is reproduced byte-for-byte by ``scripts/chips/rtl8821cu_dkms/verify_pcap.py`` — which drives this
        method synchronously with NO running loop, so that path skips the RX reader (host->chip
        only). Under a real event loop the bulk-IN RX reader starts FIRST: the monitor RX-enable
        lives inside ``cold_bringup``, and the kernel posts RX URBs before that gate — a reader
        started after it leaves the RX-DMA stalled (the chip's RX-starvation history)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.info = bringup.cold_bringup(self.transport)
            return True
        self._claim()
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8821cu-dkms-rx",
            on_fatal=lambda e: self._on_lost and self._on_lost(e))
        self._reader.start()
        # Run cold_bringup's phases individually so the UI gets progress between them. Each phase is
        # offloaded to the executor; progress_cb fires here on the loop thread (never from the
        # executor thread, which Textual can't repaint from). Order matches bringup.cold_bringup.
        def _p(frac: float, msg: str) -> None:
            if progress_cb:
                progress_cb(frac, msg)
        t = self.transport
        _p(0.05, "Reading chip ID / EFUSE")
        info = await loop.run_in_executor(None, bringup.phase_chip_info, t)
        _p(0.15, "Powering on + downloading firmware")
        await loop.run_in_executor(None, bringup.phase_fw_caps, t, info)
        _p(0.35, "MAC / BB / RF init")
        await loop.run_in_executor(None, bringup.phase_hal_init, t, info)
        _p(0.75, "Interface + channel tune")
        await loop.run_in_executor(None, bringup.phase_iface, t, info)
        _p(0.85, "Enabling monitor RX")
        await loop.run_in_executor(None, bringup.phase_monitor, t, info)
        self.info = info
        self._log_detected_config(info)
        _p(0.90, "Priming 2.4 GHz band")
        await self._prime_2g_band()
        # phydm dynamic-check watchdog (kernel-parity — its ~2 s ticks are in the pcap). DIG runs
        # here; without it the RX AGC sits at full gain and the OFDM false-alarm count floods.
        self._wd_state = watchdog.WatchdogState(
            eeprom_thermal=self.info.eeprom_thermal, thermal_offset=efuse.thermal_offset(self.info))
        self._watchdog_task = loop.create_task(self._watchdog_loop())
        _p(1.0, "RTL8821CU monitor up (ch 1 @ 20 MHz)")
        return True

    def _log_detected_config(self, info) -> None:
        """One-line log of the EFUSE-derived board burn at connect. The pcap-gated reference card is
        rfe_type_expand 0x22 (BTG / cut 4 / 1-Ant@main / combo); the runtime EFUSE branches
        (chan cut-A RF 0xb8, mac rfe-2 PAD_CTRL1, the phydm table walker's cut/rfe/package rows,
        default_rf_set AGC-diff, btc RFE decode) are ported from vendor C but only this burn is
        HW-verified, so a different burn is tagged. A 2-antenna board has no ported coex module
        (only the 1-antenna path is ported) and is called out as a genuinely untested variant."""
        rf_set = "BTG" if info.default_rf_set == phy.SWITCH_TO_BTG else "WLG"
        ant_port = "main" if info.single_ant_path == 1 else "aux"
        untested = info.rfe_type != _REF_RFE_TYPE or info.chip_ver != _REF_CUT
        logger.info(
            "RTL8821CU board: rfe_type=0x%02x rf_set=%s cut=%d %d-Ant@%s package=%d xtal=0x%02x "
            "bt_coex=%s%s", info.rfe_type, rf_set, info.chip_ver, info.ant_num, ant_port,
            info.phydm_package_type, info.crystal_cap, info.bt_coexist,
            " [untested variant]" if untested else "")
        if info.ant_num == 2:
            logger.warning("RTL8821CU: untested variant: 2-antenna board — only the 1-antenna "
                           "BT-coex path is ported; antenna routing may be wrong.")

    async def _prime_2g_band(self) -> None:
        """Re-lock 2.4 GHz after cold init. The cold ch1 tune runs before the antenna is switched to
        WiFi, so the LO never locks (RF18 BIT16 stays set) and 2.4 GHz decodes nothing. Invalidating
        ``current_band`` and re-tuning re-runs the full band switch, now post-antenna-switch; a bare
        ``RF18[16]=0`` write does not lock it. ``set_channel`` pauses the reader across the RF18
        write so it lands."""
        self.transport.current_band = None
        await self.set_channel(_PRIME_2G_CH)

    async def _watchdog_loop(self) -> None:
        """Run the phydm dynamic-check tick at the kernel ~2 s cadence (DIG / CCK-PD / RX-agg /
        thermal / env-monitor) — the runtime maintenance the kernel does and the cold path does not.
        Serialized with set_channel via _io_lock so two control-transfer sequences never interleave;
        the blocking tick is offloaded so it never stalls the RX dispatch on the event loop."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(_WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    await loop.run_in_executor(None, watchdog.tick, self.transport, self._wd_state)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8821CU watchdog stopped on error")

    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs (FCS already
        stripped) and fan each parsed dict to the rx callback."""
        cb = self._rx_cb
        if cb is None and not self._ack_detect_on:
            return
        for frame, rssi in iter_frames(buf, self.transport.cck_new_agc):
            # A 10-byte 0xD4 frame is an ACK (the parser drops control frames); the base tallies it
            # iff the ACK tap is armed and RA=frame[4:10] is a MAC we inject as.
            if len(frame) == 10 and frame[0] == 0xD4:
                self.record_ack(frame)
                continue
            if cb is not None:
                parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
                if parsed is not None:
                    cb(parsed)

    async def _enable_rx_acks(self) -> None:
        """No-op: set_opmode_monitor accept-alls RXFLTMAP1, so the recipient's ACK control frames
        (FC=0xD4) already reach RX. Nothing to enable on the chip (the base arms the tally)."""
        return

    async def _disable_rx_acks(self) -> None:
        """No-op, matching ``_enable_rx_acks``: the monitor RX filter is left untouched."""
        return

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to ``channel`` via the phydm band/channel/bandwidth set (``chan.set_channel``,
        20 MHz). Requires a prior ``connect`` (needs the cached ``info``). Under a real loop the
        tune is serialized with inject / the watchdog tick (``_io_lock``) and offloaded; the offline
        gate drives it synchronously (no running loop).

        A deliberate (non-``scan``) tune pauses the RX reader across the switch: a concurrent
        bulk-IN reverts the RF18 read-modify-write (documented HW race), and a one-shot focus/attack
        tune has nothing to re-land it, so it would otherwise strand the synth — dead RX and
        mis-tuned TX. Hopping (``scan``) self-heals (a later switch lands), so it skips the pause to
        avoid the per-hop drain. No RX is lost either way: ``chan.set_channel`` stops TRX for the
        RF18 write."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            chan.set_channel(self.transport, self.info, channel)
            return True
        async with self._io_lock:
            reader = self._reader
            pause = reader is not None and not scan
            if pause:
                await loop.run_in_executor(None, reader.pause)
            try:
                await loop.run_in_executor(None, chan.set_channel, self.transport, self.info, channel)
            finally:
                if pause:
                    reader.resume()
        return True

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        """Build the management TX descriptor for ``frame_bytes`` (HW ACK-retry limit
        RETRY_COUNT=6) and bulk-OUT [desc][frame] once. BMC is derived from the
        frame's addr1; the descriptor builder is byte-verified against the aireplay-ng capture by
        the gate's inject branch.

        Serialized under ``_io_lock`` so TX never overlaps a ``set_channel`` tune: otherwise a
        frame can go out on a half-switched / stranded synth and the AP never hears it (the
        transient 5 GHz-TX failures). The no-loop gate path bulk-outs synchronously."""
        pkt = tx.build_mgnt_txdesc(frame_bytes, qsel=_QSEL_MGNT, raid=_RAID_INJECT)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.transport.bulk_out(pkt)
            return True
        async with self._io_lock:
            await loop.run_in_executor(None, self.transport.bulk_out, pkt)
        return True

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        """Realtek HW assigns the 802.11 sequence number (the txdesc sets EN_HWSEQ), so the
        frame goes out unchanged."""
        return frame_bytes

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Point REG_MACID at ``mac`` so the hardware HW-ACKs frames addressed to it while
        staying in monitor mode — the accept-all monitor RCR (AAP) still HW-ACKs RA==REG_MACID,
        so no RCR flip is needed. MAC-only, mirroring the proven Realtek siblings. Reversed by
        exit_active_monitor. ``bssid`` is unused (register-MAC ACK is a pure RA-match)."""
        await self._write_mac(bytes(mac))
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the card's real EFUSE MAC in REG_MACID (stop ACKing the forged MAC)."""
        if self.info is not None:
            await self._write_mac(efuse.mac_address(self.info))

    async def _write_mac(self, mac6: bytes) -> None:
        """Program ``mac6`` into REG_MACID, serialized with the watchdog/set_channel (``_io_lock``)
        and offloaded so the blocking control transfers never stall the RX dispatch."""
        loop = asyncio.get_running_loop()
        async with self._io_lock:
            await loop.run_in_executor(None, mac.set_mac_addr, self.transport, mac6)

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self._reader is not None:
            await self._reader.stop()       # join the reader BEFORE releasing the USB handle
            self._reader = None
        self.transport.close()
