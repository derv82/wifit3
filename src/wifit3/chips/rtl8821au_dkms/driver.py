"""RTL8821AU / RTL8811AU driver — vendor (Lucid-Duck DKMS) cleanroom port.

Status: 2.4 GHz monitor RX complete (M1–M5). ``connect()`` runs the deterministic
bring-up — firmware download (M1) -> MAC init (M2) -> BB/RF init (M3) -> 2.4 GHz
channel tune (M4) -> the post-tune hal_init tail + phydm InitHalDm DIG/AGC/EDCCA
seed (M5 §1/§2) -> monitor opmode entry (M5 §3) — then starts the bulk-IN RX reader
(promiscuous monitor frames + per-frame 8821a RSSI) and the runtime phydm DIG/AGC
watchdog. All steps except the live EDCCA PSD search are pcap-verified byte-for-byte.

The RX reader is started **before** the monitor RCR write: the kernel posts RX URBs
before opening the gate, and this chip has RX-starvation history (see rx_reader.py).

``inject_frame`` (M6) transmits one fake-descriptor frame on bulk-OUT — deauth,
fake-auth, and WEP ARP replay all ride this one path; it is explicit-action only
(passive-by-default). TX power / EFUSE, 5 GHz (M7), and manager registration behind
``WIFIT3_RTL8821`` (M8) are later milestones. This driver is intentionally NOT
registered in ``wlan/manager.py`` yet — exercise it via ``scripts/rtl8821au_dkms/``.
Sibling to the untouched mainline ``chips/rtl8821au/``.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower
from .constants import USB_PID_AWUS036ACS, USB_VID_REALTEK
from .rx import iter_frames
from .transport import RTL8821AUDkmsTransport
from .tx import build_mgmt_txdesc

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8821au_fw.bin"
_DEFAULT_CHANNEL = 1          # connect-time tune target (matches the cold-boot capture)
_FALLBACK_CRYSTAL_CAP = 0x20  # EEPROM default if the efuse xtal byte reads blank
# 2.4 GHz, 20 MHz primary. 5 GHz tune + TX is M7. # TODO(8812au): 5 GHz band.
CHANNELS_2G = list(range(1, 14))


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8821auDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(USB_VID_REALTEK, USB_PID_AWUS036ACS,
                 "Realtek RTL8821AU/RTL8811AU 1T1R (ALFA AWUS036ACS) — vendor/DKMS port"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G

    def __init__(self, transport: RTL8821AUDkmsTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None   # efuse 0x107 (M-TXPWR)
        self._crystal_cap: int = _FALLBACK_CRYSTAL_CAP
        self._tx_power = None                    # efuse PathTxPwr (path A, 2.4 GHz)
        self._channel: Optional[int] = None
        self.is_warm: bool = False
        # Runtime DIG/AGC watchdog. Toggleable so a fixed-channel A/B can isolate the
        # watchdog's effect on RX breadth (scan_hw.py --no-dig).
        self.enable_dig: bool = True
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._dig_task: Optional[asyncio.Task] = None
        # Serializes control-transfer batches (DIG watchdog vs set_channel) so two
        # executor threads never drive EP0 at once; the RX reader uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8821auDkmsDriver":
        return cls(RTL8821AUDkmsTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up is blocking synchronous USB I/O; keep it off the event loop.

        # EFUSE read (probe phase, before power-on, like the vendor): crystal_cap (AFE
        # trim) + the per-rate TX-power base/diffs + the MAC address.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, efuse.read_chip_params, self.transport)
        self.mac_address = params.mac_address
        self._crystal_cap = params.crystal_cap
        self._tx_power = params.tx_power
        logger.info("RTL8821AU efuse: crystal_cap=0x%02x mac=%s cck_base[0]=0x%02x",
                    params.crystal_cap, params.mac_address or "<blank>",
                    params.tx_power.cck_base[0])

        if progress_cb:
            progress_cb(0.2, "Uploading firmware")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, firmware.bring_up, self.transport, fw)
        if not ready:
            logger.error("RTL8821AU firmware download did not reach FW-ready (WINTINI_RDY)")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False

        if progress_cb:
            progress_cb(0.6, "Configuring MAC / BB / RF + channel tune + TX power + phydm seed")

        # Deterministic init chain M2 -> M5 §2 (all pcap-verified except the live
        # EDCCA search). Keep in sync with scripts/rtl8821au_dkms/verify_pcap.py.
        def _init(t):
            mac.phy_mac_config(t)                     # M2: MAC register table
            mac.mac_init_misc(t)                      # M2: queue/MISC + REG_CR
            bb.phy_bb_config(t, crystal_cap=self._crystal_cap)  # M3: BB PHY_REG + AGC + xtal
            rf.phy_rf_config(t)                       # M3: RadioA
            chan.set_chnl_bw(t, _DEFAULT_CHANNEL)     # M4: 2.4 GHz band + ch + 20 MHz
            txpower.set_tx_power(t, _DEFAULT_CHANNEL, self._tx_power)  # M-TXPWR: per-rate txagc
            mac.hal_init_misc_pre(t)                  # M5 §1a: security + MISC11
            dig.init_hal_dm(t, search_edcca=True)     # M5 §2: phydm DIG/AGC/EDCCA seed
            mac.hal_init_misc_post(t)                 # M5 §1b: turn-on tail

        await loop.run_in_executor(None, _init, self.transport)
        self._channel = _DEFAULT_CHANNEL

        # M5 §5: start the bulk-IN RX reader BEFORE the monitor RCR opens the RX gate
        # (the kernel posts URBs before the gate; this chip has RX-starvation history).
        # The reader keeps a blocking bulk read posted on a dedicated thread (off the
        # event loop, so the TUI can't starve RX); each aggregated buffer is split into
        # 802.11 frames (FCS-stripped, per-frame RSSI) and fanned to the rx callback.
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8821au-dkms-rx")
        self._reader.start()

        # M5 §3: monitor opmode entry (Set_MSR NOLINK + RCR accept-all + RXFLTMAP).
        await loop.run_in_executor(None, monitor.enter_monitor, self.transport)

        # M5 §6: the runtime phydm DIG/AGC watchdog — adapt the InitHalDm IGI seed to
        # the live false-alarm rate every ~2 s (the kernel cadence). RX-side only.
        if self.enable_dig:
            self._dig_task = loop.create_task(self._dig_watchdog())
        else:
            logger.info("RTL8821AU DIG watchdog disabled (IGI stays at the InitHalDm seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz (monitor)")
        return True

    async def _dig_watchdog(self) -> None:
        """Periodic DIG watchdog. Serialized with set_channel via _io_lock."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(dig.WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    tick = await loop.run_in_executor(None, dig.watchdog_tick, self.transport)
                logger.debug("RTL8821AU DIG: IGI=0x%02x fa=%d (ofdm=%d cck=%d)",
                             tick.igi, tick.fa_cnt, tick.ofdm_fa, tick.cck_fa)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8821AU DIG watchdog stopped on error")

    # --- RX path -----------------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs and
        fan each parsed dict to the rx callback. FCS already stripped."""
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz channel at 20 MHz primary, with that channel's TX power.

        Re-runs the band + channel + BW tune (M4) then re-applies the per-rate txagc for
        the channel's power group (M-TXPWR), so a deauth/WEP run on any 2.4 GHz channel
        transmits at the correct EFUSE-calibrated power. The band switch is idempotent.
        # TODO(M7): 5 GHz.
        """
        loop = asyncio.get_running_loop()

        def _tune(t):
            chan.set_chnl_bw(t, channel)
            txpower.set_tx_power(t, channel, self._tx_power)

        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 frame (M6) — deauth, fake-auth, or WEP ARP replay.

        Builds the fake TX descriptor (M6) and sends ``[desc | frame]`` on the bulk-OUT
        pipe (ep 0x09, the MGMT queue). ``frame_bytes`` is the MPDU *without* FCS (the
        HW appends it). For WEP ARP replay the frame is already WEP-encrypted and is
        injected raw (the descriptor's SEC_TYPE = 0). Serialized with set_channel / the
        DIG watchdog via ``_io_lock`` so the frame is never emitted mid-retune. TX is
        explicit-action only (passive-by-default): nothing on the scan/connect path
        calls this.

        ``use_no_ack`` is accepted for API compatibility; the minimal fake descriptor
        uses the HW-default ACK/retry policy. TX power is the BB-default (the per-rate
        EFUSE TX-power level is a separate deferred milestone), adequate for a nearby
        target. # TODO(txpower): per-rate EFUSE TX-power level for distant targets.
        """
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast) bit
        desc = build_mgmt_txdesc(len(frame_bytes), bmc=bmc)
        async with self._io_lock:           # don't TX mid-retune (set_channel/DIG)
            await loop.run_in_executor(None, self.transport.bulk_out, desc + frame_bytes)
        return True

    async def close(self) -> None:
        # Stop the DIG watchdog and the reader before releasing the USB handle.
        if self._dig_task is not None:
            self._dig_task.cancel()
            try:
                await self._dig_task
            except asyncio.CancelledError:
                pass
            self._dig_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        self.transport.close()
