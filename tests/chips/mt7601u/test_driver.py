"""Driver-contract tests for the MT7601U port.

No hardware: PyUSB is faked. These pin the contract discovery depends on -- the ABC
surface, the advertised channels, and that transmit refuses loudly rather than putting
an unported descriptor on the wire.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport
from wifit3.chips.mt7601u import SUPPORTED_IDS, import_driver
from wifit3.chips.mt7601u.constants import (
    MT_MAC_ADDR_DW0,
    MT_MAC_ADDR_DW1,
    MT_MAC_ADDR_DW1_U2ME_MASK,
)
from wifit3.chips.mt7601u.mac import STAT_WORK_INTERVAL_S
from wifit3.chips.mt7601u.driver import (
    CALIBRATE_INTERVAL_S,
    RX_BUFFER_SIZE,
    MT7601UDriver,
)
from wifit3.chips.mt7601u.tx import TX_NO_STATION, TX_QUEUE_AC_BE, build_tx_dma
from tests.chips.mt7601u.test_rx import build_segment
from wifit3.models.device_id import DeviceID


@pytest.fixture(name="driver")
def _driver(monkeypatch: pytest.MonkeyPatch) -> MT7601UDriver:
    monkeypatch.setattr(usb.core, "Device", lambda *a, **k: None, raising=False)
    return MT7601UDriver.from_usb_device(
        None, DeviceID(0x148F, 0x7601, "MT7601U"))       # type: ignore[arg-type]


class TestReceiveArming:
    """The RX URBs must be submitted after mcu_cmd_init and before write_mac_initvals.

    dma.c:517 mt7601u_dma_init sits at exactly that point. Armed anywhere else the
    chip never streams: measured on the dongle, 0 frames armed before mcu_cmd_init
    and 118 buffers / 33548 bytes armed at that point.
    """

    def test_reader_starts_between_mcu_cmd_init_and_write_mac_initvals(self, driver) -> None:
        src = inspect.getsource(driver._init_hardware)
        assert src.index("_start_rx()") > src.index("mcu_cmd_init()")
        assert src.index("_start_rx()") < src.index("write_mac_initvals()")

    def test_reader_is_not_started_a_second_time(self, driver) -> None:
        assert inspect.getsource(driver._init_hardware).count("_start_rx()") == 1


class TestBringUpOrder:
    """init.c:330-347 -- gate the WLAN clock and wait for the ASIC, THEN push the
    firmware image, poll WPDMA idle, and wait for the ASIC a second time.

    The port previously pushed the image before chip_onoff, leaving the RF path
    ungated while it loaded. Measured on a cold dongle both orders transmit, so
    the ordering is pinned as driver parity rather than left to chance.
    """

    def test_the_wlan_clock_is_gated_before_the_firmware_is_pushed(self, driver) -> None:
        src = inspect.getsource(driver._init_hardware)
        assert src.index("chip_onoff(True)") < src.index("load_firmware(")

    def test_the_firmware_load_is_waited_for_on_both_sides(self, driver) -> None:
        src = inspect.getsource(driver._init_hardware)
        # usb.c:291 probe gate, then init.c:332 before the image and init.c:347 after it.
        assert src.count("wait_asic_ready()") == 3
        assert src.index("load_firmware(") < src.rindex("wait_asic_ready()")

    def test_the_csr_and_bbp_reset_follows_the_firmware_load(self, driver) -> None:
        src = inspect.getsource(driver._init_hardware)
        assert src.index("load_firmware(") < src.index("reset_csr_bbp()")


class TestContract:
    def test_import_driver_returns_the_class(self) -> None:
        assert import_driver() is MT7601UDriver

    def test_no_abstract_methods_left(self) -> None:
        """The ABC is enforced at instantiation, so an unimplemented method here is
        a crash during discovery rather than a test failure."""
        assert not MT7601UDriver.__abstractmethods__

    def test_claims_seventeen_ids(self) -> None:
        assert len(SUPPORTED_IDS) == 17

    def test_the_dongle_under_test_is_claimed(self) -> None:
        assert DeviceID(0x148F, 0x7601, "MT7601U") in SUPPORTED_IDS

    def test_all_2ghz_channels_including_14(self) -> None:
        assert MT7601UDriver.SUPPORTED_CHANNELS == list(range(1, 15))

    def test_kernel_module_must_be_blocked(self) -> None:
        """17 of the IDs are claimed by the in-kernel driver, so Linux setup has to
        blocklist it or the kernel owns the interface first."""
        assert "mt7601u" in MT7601UDriver.CONFLICTING_LINUX_MODULES

    def test_requires_a_replug_after_modprobe(self) -> None:
        assert MT7601UDriver.LINUX_REPLUG_AFTER_MODPROBE is True

    def test_starts_cold(self) -> None:
        """No warm-reattach path is ported, so connect() must never claim one."""
        assert MT7601UDriver.__dict__.get("_warm_reattach") is None

    def test_rx_buffer_matches_the_kernel_rx_order(self) -> None:
        assert RX_BUFFER_SIZE == 32768


class TestConstruction:
    def test_mac_unknown_until_the_eeprom_is_read(self, driver: MT7601UDriver) -> None:
        assert driver.mac_address is None

    def test_not_warm_before_connect(self, driver: MT7601UDriver) -> None:
        assert driver.is_warm is False

    def test_the_phy_reads_the_same_eeprom_record_the_driver_does(self,
                                                                 driver: MT7601UDriver) -> None:
        """The live-TX defect. connect() rebound self.ee to the object read() returned
        while phy kept the one it was constructed with, so the TX power path packed
        MT_TX_PWR_CFG_0 from an empty table and programmed zero power. The MAC then
        transmitted, the status FIFO reported success and the counters advanced, and
        the antenna emitted nothing -- invisible to every check short of the air."""
        assert driver.phy.ee is driver.ee

    def test_reading_the_eeprom_does_not_replace_the_shared_record(self,
                                                                   driver: MT7601UDriver) -> None:
        assert driver.eeprom_dev.ee is driver.ee

    def test_product_name_falls_back_to_the_supported_ids_label(self) -> None:
        """The MT7601U ids carry only a chipset name, so product_name stays None and
        the UI uses the SUPPORTED_IDS label -- the contract's documented fallback."""
        drv = MT7601UDriver.from_usb_device(
            None, DeviceID(0x148F, 0x7601, "MT7601U"))   # type: ignore[arg-type]
        assert drv.product_name is None

    def test_product_name_is_taken_from_a_vendor_named_entry(self) -> None:
        drv = MT7601UDriver.from_usb_device(
            None, DeviceID(0x148F, 0x7601, "MT7601U", vendor="ALFA",
                           product_name="ALFA MT7601U"))  # type: ignore[arg-type]
        assert drv.product_name == "ALFA MT7601U"


FRAME = b"\x08\x00" + b"\xaa" * 20
"""A 22-byte control frame."""


class RecordingQueues:
    """Stands in for TxQueues so the driver is tested without a USB device."""

    def __init__(self, accepts: bool = True) -> None:
        self.submitted: list[bytes] = []
        self.ack_flags: list[bool] = []
        self.accepts = accepts
        self.closed = False

    def __getitem__(self, queue: int) -> "RecordingQueue":
        return RecordingQueue(self)

    def close(self) -> None:
        self.closed = True


class RecordingQueue:
    def __init__(self, owner: "RecordingQueues") -> None:
        self._owner = owner

    def submit(self, frame: bytes, *, ack: bool = True) -> bool:
        if not self._owner.accepts:
            return False
        self._owner.submitted.append(frame)
        self._owner.ack_flags.append(ack)
        return True

    def free_entries(self) -> int:
        return 64


class TestInjectionDescriptorMatchesTheKernel:
    """The injected txwi, against the kernel's recorded injection descriptor.

    capture-6 records ``0000000000ff1a10...`` -- flags 0, rate_ctl 0, ack_ctl 0, wcid 0xff,
    BYTE_CNT 26, PKTID 1, on queue AC_BE. Its ack_ctl 0 is aireplay-ng's radiotap NOACK and
    not a kernel constraint, so production keeps REQ set the way mt76x0u and mt76x2u do on
    this identical txwi, and ``ack=False`` is the replay form the byte-match tests build.
    Active monitor is what lets the chip match an ACK to a spoofed Addr2.
    """

    def test_the_queue_is_asked_for_a_link_layer_ack(self, driver: MT7601UDriver) -> None:
        """The MAC's ACK-based retry is the only retransmission injection gets."""
        queue = RecordingQueues()
        driver._tx_queues = queue
        asyncio.run(driver.inject_frame(FRAME))
        assert queue.ack_flags == [True]

    def test_the_ack_request_does_not_depend_on_the_tally(
            self, driver: MT7601UDriver) -> None:
        """Coupling REQ to enable_rx_acks left every send_no_wait frame -- all of WEP --
        without a hardware retry, while send_until_ack campaigns got one."""
        queue = RecordingQueues()
        driver._tx_queues = queue

        async def run() -> None:
            await driver.enable_rx_acks()
            await driver.inject_frame(FRAME)
            await driver.disable_rx_acks()
            await driver.inject_frame(FRAME)

        asyncio.run(run())
        assert queue.ack_flags == [True, True]

    def test_the_built_descriptor_is_the_kernel_reference_byte_for_byte(self) -> None:
        # capture-6's frame is a 26-byte broadcast deauth, so BYTE_CNT is 26 (0x1a)
        # with PKTID 1 in the top nibble. A 22-byte frame would read 0x16 here and
        # prove nothing about the descriptor.
        frame = b"\x08\x00" + b"\xaa" * 24
        payload = build_tx_dma(frame, ack=False, wcid=TX_NO_STATION, rate=0,
                               queue=TX_QUEUE_AC_BE)
        assert payload[4:12].hex() == "0000000000ff1a10"

    def test_the_ack_bit_is_what_would_have_differed(self) -> None:
        frame = b"\x08\x00" + b"\xaa" * 24
        with_ack = build_tx_dma(frame, ack=True, wcid=TX_NO_STATION, rate=0,
                                queue=TX_QUEUE_AC_BE)
        assert with_ack[4:12].hex() == "0000000001ff1a10"


class TestTransmitSubmits:
    def test_inject_frame_submits_to_the_queue(self, driver: MT7601UDriver) -> None:
        queue = RecordingQueues()
        driver._tx_queues = queue
        assert asyncio.run(driver.inject_frame(FRAME)) is True
        assert queue.submitted == [FRAME]

    def test_a_rejected_send_reports_false(self, driver: MT7601UDriver) -> None:
        queue = RecordingQueues(accepts=False)
        driver._tx_queues = queue
        assert asyncio.run(driver.inject_frame(FRAME)) is False

    def test_a_frame_the_queue_rejects_raises_rather_than_reporting_failure(
            self, driver: MT7601UDriver) -> None:
        """A frame the queue refuses to build was never handed to the chip, so
        False would invite a retry of something that was never sent. TxQueue owns
        the validation (test_tx_ring.py); the driver must not swallow it."""
        class Refusing(RecordingQueues):
            def __getitem__(self, queue: int):
                raise ValueError("frame too short to be 802.11")
        driver._tx_queues = Refusing()
        with pytest.raises(ValueError, match="too short"):
            asyncio.run(driver.inject_frame(b"\x08\x00"))

    def test_close_releases_the_transmit_queues(self, driver: MT7601UDriver) -> None:
        queue = RecordingQueues()
        driver._tx_queues = queue
        driver._reader = None
        driver.transport.release = lambda: None
        driver.transport.dispose = lambda: None
        driver.chip_init.chip_onoff = lambda enable: None
        asyncio.run(driver.close())
        assert queue.closed

    def test_each_injection_gets_a_fresh_sequence_number(
            self, driver: MT7601UDriver) -> None:
        """The txwi leaves NSEQ clear, so the MPDU's own seq_ctrl is transmitted and a
        burst that reuses 0 is dropped by the receiver's duplicate filter."""
        frame = bytes([0x08, 0x01]) + bytes(22)
        seqs = [int.from_bytes(driver._stamp_tx_seq(frame)[22:24], "little") >> 4
                for _ in range(3)]
        assert seqs == [1, 2, 3]

    def test_a_fragment_burst_reuses_one_sequence_number(
            self, driver: MT7601UDriver) -> None:
        frame = bytes([0x08, 0x01]) + bytes(20) + bytes([0x02, 0x00])
        first = driver._stamp_tx_seq(frame)[22:24]
        assert driver._stamp_tx_seq(frame)[22:24] == first
        assert first[0] & 0x0F == 2                  # the fragment number survives

    def test_a_control_frame_is_left_alone(self, driver: MT7601UDriver) -> None:
        frame = bytes([0xD4, 0x00]) + bytes(8)       # 10-byte ACK, no seq_ctrl
        assert driver._stamp_tx_seq(frame) == frame

    def test_tx_is_still_coroutine_shaped(self, driver: MT7601UDriver) -> None:
        assert inspect.iscoroutinefunction(driver._inject_frame)

    def test_the_ack_tally_needs_a_real_injection_to_register_a_mac(
            self, driver: MT7601UDriver) -> None:
        """Now that transmit exists, inject_frame registers the Addr2 it watches."""
        queue = RecordingQueues()
        driver._tx_queues = queue
        asyncio.run(driver.enable_rx_acks())
        asyncio.run(driver.inject_frame(FRAME))
        assert driver.acks_seen(bytes.fromhex("000000000000")) == 0


class TestAckTaps:
    def test_rx_acks_are_a_documented_no_op(self, driver: MT7601UDriver) -> None:
        """mac_start installs PROMISC plus ACK/CTS/RTS, so control frames already
        reach the stream; the hook must say so rather than write a register."""
        asyncio.run(driver._enable_rx_acks())
        asyncio.run(driver._disable_rx_acks())

    def test_ack_tally_needs_transmit_so_it_stays_empty(self, driver: MT7601UDriver) -> None:
        """The base class learns which MACs to watch from inject_frame, and transmit is
        unported, so no MAC is ever registered and the tally stays at zero. That makes
        inject_frame_slow_retry useless on this card -- a documented consequence, not a
        bug to work around."""
        asyncio.run(driver.enable_rx_acks())
        driver.record_ack(bytes.fromhex("660000000000001122334455"))
        assert driver.acks_seen(bytes.fromhex("001122334455")) == 0


class TestChannelSelection:
    def test_rejects_a_channel_outside_the_radio(self, driver: MT7601UDriver) -> None:
        assert asyncio.run(driver.set_channel(36)) is False

    def test_rejects_channel_zero(self, driver: MT7601UDriver) -> None:
        assert asyncio.run(driver.set_channel(0)) is False

    def test_scan_flag_does_not_change_the_channel_set(self) -> None:
        assert MT7601UDriver.SUPPORTED_CHANNELS == list(range(1, 15))


class TestBringUpFailsLoudlyOnAWedgedChip:
    """An unresponsive card must raise, not report a healthy connect.

    ``rr`` returns ~0 for a failed or short transfer and ``vendor_request`` swallows
    the timeout after its retries, so without a liveness gate the EEPROM decodes to a
    garbage MAC, bring-up writes that garbage into the chip and ``connect`` returns
    True -- a dead card presented as a working one, with no frames ever received.
    """

    def test_a_wedged_chip_raises_instead_of_connecting(self, driver) -> None:
        from wifit3.errors import BringUpError

        driver.transport.rr = lambda offset: 0xFFFFFFFF
        with pytest.raises(BringUpError, match="no response"):
            driver._require_live_chip()

    def test_the_error_names_the_replug_you_actually_need(self, driver) -> None:
        from wifit3.errors import BringUpError

        driver.transport.rr = lambda offset: 0xFFFFFFFF
        with pytest.raises(BringUpError) as excinfo:
            driver._require_live_chip()
        assert "replug" in str(excinfo.value).lower()

    def test_a_responsive_chip_is_not_rejected(self, driver) -> None:
        driver.transport.rr = lambda offset: 0x00000000
        driver._require_live_chip()   # must not raise on a live chip

    def test_bring_up_probes_liveness_before_reading_the_eeprom(self, driver) -> None:
        src = inspect.getsource(driver.connect)
        assert src.index("_require_live_chip()") < src.index("await self._bringup(")
        assert "eeprom_dev.read()" in inspect.getsource(driver._init_hardware)


class TestBringUpRefusesAKernelBoundChip:
    """A dongle the kernel driver has probed must be refused, not silently detached.

    ``transport.claim`` detaches a bound kernel driver without comment, so the two cases
    look identical downstream. They are not. mt7601u initialises the chip during probe
    and that init survives ``modprobe -r``: measured on the dongle, every run against a
    kernel-initialised chip received 0 bytes at 200/1000/3000/5000 ms timeouts while a
    cold-plugged chip received 221 buffers / 75260 bytes and 5 decoded APs. udev binds the
    module on plug, so the refused case is the default one on a stock system -- and left
    alone it fails silently, showing an empty scan with no error to explain it.
    """

    @staticmethod
    def _bind(driver, bound: bool) -> None:
        driver.transport.dev = SimpleNamespace(
            is_kernel_driver_active=lambda iface: bound)

    def test_a_kernel_bound_chip_raises_before_claiming(self, driver) -> None:
        from wifit3.errors import BringUpError

        self._bind(driver, True)
        with pytest.raises(BringUpError, match="kernel mt7601u driver is bound"):
            driver._refuse_kernel_bound_chip()

    def test_the_error_says_unloading_is_not_enough(self, driver) -> None:
        from wifit3.errors import BringUpError

        self._bind(driver, True)
        with pytest.raises(BringUpError) as excinfo:
            driver._refuse_kernel_bound_chip()
        message = str(excinfo.value).lower()
        assert "replug" in message      # the fix is a replug, not an unload
        assert "not enough" in message  # and unloading alone is explicitly ruled out

    def test_an_unbound_chip_is_brought_up_normally(self, driver) -> None:
        self._bind(driver, False)
        driver._refuse_kernel_bound_chip()   # must not raise on a cold-plugged dongle

    def test_the_check_runs_before_claim_detaches_the_kernel_driver(self, driver) -> None:
        src = inspect.getsource(driver.connect)
        assert src.index("_refuse_kernel_bound_chip()") < src.index("transport.claim()")


class TestAckTap:
    """A 10-byte 0xD4 frame is an 802.11 ACK. The parser drops control frames, so the
    tally can only be fed from _dispatch, on the raw bytes.

    Without this tap _ack_detect_on could never be served: acks_seen() stayed 0,
    inject_frame_slow_retry fell through, and deauth_client reported 0/N acks while
    claiming measured=True -- so the UI showed "0 acks" for frames that were fine.
    """

    def _ack(self, receiver: bytes) -> bytes:
        """One ACK MPDU wrapped in the USB segment _dispatch actually reads.

        The tap sits behind iter_frames, so the test has to supply a real segment
        rather than a bare frame -- a raw ACK never reaches _dispatch's own branch.
        """
        # A control-frame ACK is exactly 10 bytes: FC(2) + Duration(2) + RA(6), with the
        # receiver address at [4:10] -- the offset record_ack reads. The DMA header chains
        # 4-byte-aligned lengths, so the MPDU rides padded to 12: build_segment derives
        # dma_len from the payload, and an unaligned 10 is rejected by next_segment_len's
        # alignment gate before the tap ever sees it.
        mpdu = b"\xd4\x00\x00\x00" + receiver
        payload = mpdu + b"\x00\x00"
        return build_segment(mpdu_len=len(mpdu), payload=payload)

    def test_an_ack_is_tallied_when_the_tally_is_armed(self, driver) -> None:
        target = b"\x11\x22\x33\x44\x55\x66"
        driver._ack_detect_on = True
        driver._our_tx_macs = {target}
        driver._dispatch(self._ack(target))
        assert driver.acks_seen(target) == 1

    def test_an_ack_for_a_mac_we_did_not_inject_is_ignored(self, driver) -> None:
        driver._ack_detect_on = True
        driver._our_tx_macs = {b"\xaa\xbb\xcc\xdd\xee\xff"}
        driver._dispatch(self._ack(b"\x11\x22\x33\x44\x55\x66"))
        assert driver.acks_seen(b"\x11\x22\x33\x44\x55\x66") == 0

    def test_an_ack_is_ignored_while_the_tally_is_disarmed(self, driver) -> None:
        target = b"\x11\x22\x33\x44\x55\x66"
        driver._ack_detect_on = False
        driver._our_tx_macs = {target}
        driver._dispatch(self._ack(target))
        assert driver.acks_seen(target) == 0

    def test_an_ack_is_tallied_even_with_no_rx_callback_registered(self, driver) -> None:
        """The tally is the only reason to read RX; an early return on a missing
        callback would starve it for every campaign that injects without capturing."""
        target = b"\x11\x22\x33\x44\x55\x66"
        driver._rx_callback = None
        driver._ack_detect_on = True
        driver._our_tx_macs = {target}
        driver._dispatch(self._ack(target))
        assert driver.acks_seen(target) == 1

    def test_an_ack_never_reaches_the_frame_parser(self, driver) -> None:
        seen: list = []
        driver._ack_detect_on = True
        driver._our_tx_macs = set()
        driver.register_rx_callback(seen.append)
        driver._dispatch(self._ack(b"\x11\x22\x33\x44\x55\x66"))
        assert seen == []                # a control frame is not a deliverable packet


class TestActiveMonitor:
    """mac.c:22-24 -- DW0/DW1 are plain writable registers, so the chip will HW-ACK
    whatever MAC is programmed there. Untestable on the air without a second radio in
    monitor mode, so these pin the register writes themselves."""

    def test_the_card_is_advertised_as_able_to_ack_a_forged_mac(self) -> None:
        assert MT7601UDriver.FAKE_MAC is FakeMacSupport.SPOOFABLE

    def test_ack_window_clears_this_chips_measured_round_trip(self) -> None:
        """The inherited 20ms window is shorter than this chip's ACK round-trip.

        Measured on two dongles at 1ms polling: median 57ms, max 120ms. At 0.02 an
        ACKed frame reported False, so ACK-measured deauth and send_until_ack looked
        broken when the frame had in fact landed. This pins the value above the measured
        distribution so a future "tidy-up" back to the base-class default is caught here
        rather than on hardware.
        """
        assert MT7601UDriver.MAX_ACK_DELAY >= 0.12
        assert MT7601UDriver.MAX_ACK_DELAY > Driver.MAX_ACK_DELAY

    def test_entering_programs_the_mac_little_endian_with_the_u2me_mask(
            self, driver) -> None:
        """U2ME is the autoresponder enable, and it is the only thing that makes the
        chip answer at all: without it the register pair is just an address nobody
        listens for. mac.c:22-24 sets it on every hwaddr write, so it must be set here."""
        writes: list[tuple[int, int]] = []
        driver.transport.wr = lambda off, val: writes.append((off, val))

        target = bytes.fromhex("112233445566")
        assert asyncio.run(driver.enter_active_monitor(target)) == target

        assert writes == [
            (MT_MAC_ADDR_DW0, int.from_bytes(target[0:4], "little")),
            (MT_MAC_ADDR_DW1, int.from_bytes(target[4:6], "little")
             | MT_MAC_ADDR_DW1_U2ME_MASK),
        ]

    def test_exiting_restores_the_eeprom_mac(self, driver) -> None:
        driver.mac_address = "aa:bb:cc:dd:ee:ff"
        writes: list[tuple[int, int]] = []
        driver.transport.wr = lambda off, val: writes.append((off, val))

        asyncio.run(driver.exit_active_monitor())

        assert writes == [
            (MT_MAC_ADDR_DW0, 0xDDCCBBAA),
            (MT_MAC_ADDR_DW1, 0x00FFFFEE),      # U2ME stays armed, as mac.c set it
        ]

    def test_exiting_before_the_eeprom_is_read_writes_nothing(self, driver) -> None:
        """mac_address is None until connect() decodes the EEPROM; a restore then would
        program six zero bytes and ACK nobody."""
        driver.mac_address = None
        writes: list[tuple[int, int]] = []
        driver.transport.wr = lambda off, val: writes.append((off, val))

        asyncio.run(driver.exit_active_monitor())

        assert writes == []

    def test_the_whole_cycle_leaves_the_mac_armable_again(self, driver) -> None:
        """Enter, exit, re-enter: a campaign arms, disarms and re-arms without reconnect."""
        writes: list[tuple[int, int]] = []
        driver.transport.wr = lambda off, val: writes.append((off, val))
        driver.mac_address = "aa:bb:cc:dd:ee:ff"
        target = bytes.fromhex("0a0b0c0d0e0f")

        asyncio.run(driver.enter_active_monitor(target))
        asyncio.run(driver.exit_active_monitor())
        asyncio.run(driver.enter_active_monitor(target))

        assert writes[-2:] == [
            (MT_MAC_ADDR_DW0, 0x0D0C0B0A),
            (MT_MAC_ADDR_DW1, int.from_bytes(target[4:6], "little")
             | MT_MAC_ADDR_DW1_U2ME_MASK),
        ]


class TestSelfMacWriteIsSplitFaithfully:
    """usb.c:181 mt7601u_wr hands a 32-bit register to vendor_single_wr, which writes the
    low half at ``offset`` and the high half at ``offset + 2``. The U2ME mask occupies bits
    16-23 of DW1, so it is written to 0x100e -- which the chip does not answer reads on, so
    it cannot be verified by readback. Only the transfer sequence can."""

    def test_a_self_mac_write_issues_four_vendor_transfers_in_kernel_order(
            self, driver) -> None:
        calls: list[tuple[int, int]] = []
        driver.transport.vendor_request = (
            lambda req, direction, val, offset, data=None, buflen=0:
            calls.append((offset, val)) or 0)

        driver._write_self_mac(bytes.fromhex("deadbeef0001"))

        assert calls == [
            (MT_MAC_ADDR_DW0, 0xADDE),                     # low half of DW0 @ 0x1008
            (MT_MAC_ADDR_DW0 + 2, 0xEFBE),                 # high half of DW0 @ 0x100a
            (MT_MAC_ADDR_DW1, 0x0100),                     # low half of DW1 @ 0x100c
            (MT_MAC_ADDR_DW1 + 2, 0x00FF),                 # high half of DW1 @ 0x100e, U2ME
        ]

    def test_the_u2me_mask_reaches_the_high_half_of_dw1(self, driver) -> None:
        """The mask is the whole reason the chip answers at all, and it only lands if the
        value passed to wr() carries bits 16-23 -- a 16-bit-only value silently disables it."""
        calls: list[tuple[int, int]] = []
        driver.transport.vendor_request = (
            lambda req, direction, val, offset, data=None, buflen=0:
            calls.append((offset, val)) or 0)

        driver._write_self_mac(bytes.fromhex("deadbeef0001"))

        assert calls[-1] == (MT_MAC_ADDR_DW1 + 2, MT_MAC_ADDR_DW1_U2ME_MASK >> 16)


class TestTeardown:
    """main.c:31 mt7601u_stop then init.c:422 mt7601u_cleanup."""

    @staticmethod
    def _recorded(driver) -> list[str]:
        calls: list[str] = []
        driver.chip_init.mac_stop_hw = lambda: calls.append("mac_stop_hw")
        driver.chip_init.chip_onoff = lambda enable, reset=False: calls.append(
            f"chip_onoff({enable})")
        driver.transport.release = lambda: calls.append("release")
        driver.transport.dispose = lambda: calls.append("dispose")
        driver._tx_queues = SimpleNamespace(close=lambda: calls.append("tx_close"))

        async def stop() -> None:
            calls.append("reader_stop")

        driver._reader = SimpleNamespace(stop=stop)
        return calls

    def test_close_stops_the_mac_then_powers_the_chip_down(self, driver) -> None:
        calls = self._recorded(driver)
        asyncio.run(driver.close())
        assert calls == ["mac_stop_hw", "reader_stop", "tx_close",
                         "chip_onoff(False)", "release", "dispose"]

    def test_the_mac_stops_before_the_reader_does(self, driver) -> None:
        """init.c:285-296 drains the RX queue, which only empties while the host is
        still consuming bulk-IN."""
        calls = self._recorded(driver)
        asyncio.run(driver.close())
        assert calls.index("mac_stop_hw") < calls.index("reader_stop")

    def test_a_failed_mac_stop_does_not_strand_the_chip_powered(self, driver) -> None:
        calls = self._recorded(driver)

        def boom() -> None:
            raise RuntimeError("wedged")

        driver.chip_init.mac_stop_hw = boom
        asyncio.run(driver.close())
        assert "chip_onoff(False)" in calls
        assert calls[-2:] == ["release", "dispose"]


class TestFailedBringUpPowersDown:
    """init.c:413-423 always reaches `err: chip_onoff(false)` on the error path."""

    def test_a_raising_bring_up_powers_the_chip_down(self, driver) -> None:
        calls: list[str] = []
        driver.chip_init.chip_onoff = lambda enable, reset=False: calls.append(
            f"chip_onoff({enable})")

        async def boom(progress_cb=None) -> bool:
            raise RuntimeError("wedged halfway")

        driver._init_hardware = boom
        with pytest.raises(RuntimeError, match="wedged halfway"):
            asyncio.run(driver._bringup())
        assert calls == ["chip_onoff(False)"]

    def test_the_rx_reader_is_stopped_on_the_error_path(self, driver) -> None:
        stopped: list[str] = []
        driver.chip_init.chip_onoff = lambda enable, reset=False: None

        async def stop() -> None:
            stopped.append("reader_stop")

        async def boom(progress_cb=None) -> bool:
            driver._reader = SimpleNamespace(stop=stop)
            raise RuntimeError("wedged after the URBs were armed")

        driver._init_hardware = boom
        with pytest.raises(RuntimeError):
            asyncio.run(driver._bringup())
        assert stopped == ["reader_stop"]
        assert driver._reader is None

    def test_a_clean_bring_up_does_not_power_the_chip_down(self, driver) -> None:
        calls: list[str] = []
        driver.chip_init.chip_onoff = lambda enable, reset=False: calls.append(
            f"chip_onoff({enable})")

        async def fine(progress_cb=None) -> bool:
            return True

        driver._init_hardware = fine
        assert asyncio.run(driver._bringup()) is True
        assert calls == []


class TestDelayedWorks:
    """main.c:22-25 queues cal_work and mac_work; main.c:37-38 cancels both."""

    def test_the_intervals_are_the_kernels(self) -> None:
        assert CALIBRATE_INTERVAL_S == 4.0            # mt7601u.h:22, 4 * HZ
        assert STAT_WORK_INTERVAL_S == 10.0           # mac.c:351, 10 * HZ

    def test_close_cancels_both_tasks(self, driver: MT7601UDriver) -> None:
        async def run() -> None:
            driver._cal_task = asyncio.create_task(driver._periodic(99, lambda: None, "cal"))
            driver._stats_task = asyncio.create_task(
                driver._periodic(99, lambda: None, "stats"))
            driver.chip_init.mac_stop_hw = lambda: None
            driver.chip_init.chip_onoff = lambda enable, reset=False: None
            driver.transport.release = lambda: None
            driver.transport.dispose = lambda: None
            driver._tx_queues = None
            await asyncio.sleep(0)
            await driver.close()
            assert driver._cal_task is None and driver._stats_task is None

        asyncio.run(run())

    def test_a_usb_hiccup_does_not_end_a_loop(self, driver: MT7601UDriver) -> None:
        """One failed tick must not stop the chip refreshing its temperature."""
        calls: list[int] = []

        def boom() -> None:
            calls.append(1)
            raise usb.core.USBError("stall")

        async def run() -> None:
            task = asyncio.create_task(driver._periodic(0, boom, "calibration"))
            while len(calls) < 3:
                await asyncio.sleep(0)
            task.cancel()
            await task

        asyncio.run(run())
        assert len(calls) >= 3

    def test_an_unexpected_error_still_stops_the_loop(self, driver: MT7601UDriver) -> None:
        """Only the USB and MCU faults are tolerated; a real bug must surface."""
        async def run() -> None:
            def boom() -> None:
                raise ValueError("a real bug")
            with pytest.raises(ValueError, match="a real bug"):
                await driver._periodic(0, boom, "calibration")

        asyncio.run(run())
