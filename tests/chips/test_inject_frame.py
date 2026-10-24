"""Every driver's real ``inject_frame`` path, captured at the USB bulk-OUT write: the 802.11 bytes
ride unmodified behind the TX descriptor, a sequence number is stamped, and the no-ACK bit tracks
addr1's group bit."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest

from wifit3 import chips
from wifit3.chips.driver import Driver
from wifit3.dot11.auth_assoc import auth_req
from wifit3.dot11.deauth import build_deauth
from wifit3.dot11.mac import BROADCAST_MAC
from wifit3.dot11.probe import probe_req
from wifit3.models.device_id import DeviceID

from wifit3.chips import (
    mt7601u, mt76x0u, mt76x2u, mt7921au, mt7925au, rt2500usb, rt2800usb, rt5572, rtl8187,
    rtl8188eus, rtl8812au, rtl8812au_dkms, rtl8814au_dkms, rtl8821au, rtl8821au_dkms, rtl8821cu_dkms, rtl8822bu, rtl8822bu_dkms, rtl8822cu,
    rtl8192fu, rtl8922au, rtw88_8814au,
)
from wifit3.chips.ar9271_v2 import constants as ar9271_constants
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.mt7601u.constants import MT_TXWI_ACK_CTL_REQ as MT7601U_ACK_CTL_REQ
from wifit3.chips.mt7601u.driver import MT7601UDriver
from wifit3.chips.mt76x0u.constants import MT_TXWI_ACK_CTL_REQ as MT76X0U_ACK_CTL_REQ
from wifit3.chips.mt76x0u.driver import MT76x0UDriver
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.chips.mt76x2u.tx import _TXWI_ACK_CTL_REQ as MT76X2U_ACK_CTL_REQ
from wifit3.chips.mt7921au.constants import MT_TXD3_NO_ACK as MT7921AU_TXD3_NO_ACK
from wifit3.chips.mt7921au.driver import MT7921AUDriver
from wifit3.chips.mt7925au.constants import MT_TXD3_NO_ACK as MT7925AU_TXD3_NO_ACK
from wifit3.chips.mt7925au.driver import MT7925AUDriver
from wifit3.chips.rt2500usb.constants import TXD_W0_NEW_SEQ as RT2500USB_TXD_W0_NEW_SEQ
from wifit3.chips.rt2500usb.driver import RT2500USBDriver
from wifit3.chips.rt2800usb.constants import RT_RT5390 as RT2800USB_RT5390
from wifit3.chips.rt2800usb.constants import TXWI_W1_ACK as RT2800USB_TXWI_W1_ACK
from wifit3.chips.rt2800usb.driver import RT2800USBDriver
from wifit3.chips.rt2800usb.mac import ChipId as RT2800USBChipId
from wifit3.chips.rt3070.constants import TXWI_W1_ACK as RT3070_TXWI_W1_ACK
from wifit3.chips.rt3070.driver import RT3070Driver
from wifit3.chips.rt3070.transport import RT3070Transport
from wifit3.chips.rt5370.constants import TXWI_W1_ACK as RT5370_TXWI_W1_ACK
from wifit3.chips.rt5370.driver import RT5370Driver
from wifit3.chips.rt5370.transport import RT5370Transport
from wifit3.chips.rt5372.constants import TXWI_W1_ACK as RT5372_TXWI_W1_ACK
from wifit3.chips.rt5372.driver import RT5372Driver
from wifit3.chips.rt5372.transport import RT5372Transport
from wifit3.chips.rt5572.constants import RT_RT5592 as RT5572_RT5592
from wifit3.chips.rt5572.constants import TXWI_W1_ACK as RT5572_TXWI_W1_ACK
from wifit3.chips.rt5572.driver import RT5572Driver
from wifit3.chips.rt5572.mac import ChipId as RT5572ChipId
from wifit3.chips.rtl8187.driver import RTL8187Driver
from wifit3.chips.rtl8187.mac import ChipVariant as RTL8187ChipVariant
from wifit3.chips.rtl8188eus.constants import TXDESC_BROADMULTICAST as RTL8188EUS_BROADMULTICAST
from wifit3.chips.rtl8192fu import constants as RTL8192FU_CONSTANTS
from wifit3.chips.rtl8192fu.core import rtl8xxxu_config_endpoints_no_sie, rtl8xxxu_init_queue_priority
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.chips.rtl8188eus.driver import RTL8188EUSDriver
from wifit3.chips.rtl8188eus_dkms.constants import BMC as RTL8188EUS_DKMS_BMC
from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver
from wifit3.chips.rtl8188eus_dkms.transport import Rtl8188eusTransport
from wifit3.chips.rtl8812au.driver import RTL8812AUDriver
from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver
from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver
from wifit3.chips.rtl8821au.driver import RTL8821AUDriver
from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8822bu.driver import RTL8822BUDriver
from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.chips.rtl8922au.driver import RTL8922AUDriver
from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver

AP_MAC = bytes.fromhex("020000000001")
STATION_MAC = bytes.fromhex("020000000002")


class RecordingUsbDevice(MagicMock):
    """A PyUSB device stand-in that records every bulk-OUT ``write`` and answers control reads
    with zeros."""

    def __init__(self) -> None:
        super().__init__()
        self.bulk_writes: list[bytes] = []
        self.write.side_effect = self._record_write
        self.ctrl_transfer.side_effect = self._answer_ctrl_transfer

    def _get_child_mock(self, **kwargs: object) -> MagicMock:
        return MagicMock(**kwargs)

    def _record_write(self, endpoint: int, data: bytes, timeout: Optional[int] = None) -> int:
        self.bulk_writes.append(bytes(data))
        return len(data)

    @staticmethod
    def _answer_ctrl_transfer(bmRequestType: int, bRequest: int, wValue: int = 0, wIndex: int = 0,
                              data_or_wLength: object = None, timeout: Optional[int] = None):
        if isinstance(data_or_wLength, int):
            return bytes(data_or_wLength)
        return len(data_or_wLength) if data_or_wLength is not None else 0


@dataclass(frozen=True)
class DescriptorBit:
    word_offset: int          # byte offset of the little-endian u32 that holds the bit
    mask: int

    def is_set(self, usb_payload: bytes) -> bool:
        return bool(struct.unpack_from("<I", usb_payload, self.word_offset)[0] & self.mask)


@dataclass(frozen=True)
class DriverCase:
    build: Callable[[RecordingUsbDevice], Driver]
    payload_offset: int                       # 802.11 frame start in the bulk-OUT payload
    data_payload_offset: int                  # same, for a data-type frame
    hw_seq_bit: Optional[DescriptorBit]       # None: the driver must stamp seq_ctrl itself
    seq_stamped_by_firmware: bool = False     # neither: the firmware stamps it (measured on air)
    no_ack_bit: Optional[DescriptorBit] = None
    ack_request_bit: Optional[DescriptorBit] = None   # inverse polarity: set = wait for ACK


def first_id(supported_ids: list[DeviceID]) -> DeviceID:
    return supported_ids[0]


def build_ar9271_v2(dev: RecordingUsbDevice) -> Driver:
    transport = AR9271Transport(dev)
    endpoints = {ar9271_constants.WMI_MGMT_SVC: 5, ar9271_constants.WMI_DATA_BE_SVC: 6}
    return AR9271V2Driver.for_replay(MagicMock(t=transport), None, endpoints)


def build_mt7601u(dev: RecordingUsbDevice) -> Driver:
    driver = MT7601UDriver.from_usb_device(dev, first_id(mt7601u.SUPPORTED_IDS))
    driver.transport.out_eps = [0x08, 0x04, 0x05, 0x06, 0x07, 0x09]
    return driver


def build_mt76x0u(dev: RecordingUsbDevice) -> Driver:
    return MT76x0UDriver.from_usb_device(dev, first_id(mt76x0u.SUPPORTED_IDS))


def build_mt76x2u(dev: RecordingUsbDevice) -> Driver:
    return MT76x2UDriver.from_usb_device(dev, first_id(mt76x2u.SUPPORTED_IDS))


def build_mt7921au(dev: RecordingUsbDevice) -> Driver:
    return MT7921AUDriver.from_usb_device(dev, first_id(mt7921au.SUPPORTED_IDS))


def build_mt7925au(dev: RecordingUsbDevice) -> Driver:
    return MT7925AUDriver.from_usb_device(dev, first_id(mt7925au.SUPPORTED_IDS))


def build_rt2500usb(dev: RecordingUsbDevice) -> Driver:
    driver = RT2500USBDriver.from_usb_device(dev, first_id(rt2500usb.SUPPORTED_IDS))
    driver._bulk_out_ep = 0x01
    return driver


def build_rt2800usb(dev: RecordingUsbDevice) -> Driver:
    driver = RT2800USBDriver.from_usb_device(dev, first_id(rt2800usb.SUPPORTED_IDS))
    driver.chip_id = RT2800USBChipId(raw=RT2800USB_RT5390 << 16, silicon_id=RT2800USB_RT5390,
                                     revision=0, name="RT5390", is_supported=True)
    return driver


def build_rt3070(dev: RecordingUsbDevice) -> Driver:
    driver = RT3070Driver(RT3070Transport(dev))
    driver._bulk_out_ep = 0x01
    return driver


def build_rt5370(dev: RecordingUsbDevice) -> Driver:
    driver = RT5370Driver(RT5370Transport(dev))
    driver._bulk_out_ep = 0x01
    return driver


def build_rt5372(dev: RecordingUsbDevice) -> Driver:
    driver = RT5372Driver(RT5372Transport(dev))
    driver._bulk_out_ep = 0x01
    return driver


def build_rt5572(dev: RecordingUsbDevice) -> Driver:
    driver = RT5572Driver.from_usb_device(dev, first_id(rt5572.SUPPORTED_IDS))
    driver.chip_id = RT5572ChipId(raw=RT5572_RT5592 << 16, silicon_id=RT5572_RT5592,
                                  revision=0, name="RT5592", is_supported=True)
    return driver


def build_rtl8187(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8187Driver.from_usb_device(dev, first_id(rtl8187.SUPPORTED_IDS))
    driver._bulk_out_max_packet_sizes = {0x02: 512}
    driver.chip_variant = RTL8187ChipVariant(hwver_raw=0, name="RTL8187vB", is_rtl8187b=False,
                                             hw_rev=None)
    return driver


def build_rtl8188eus(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8188EUSDriver.from_usb_device(dev, first_id(rtl8188eus.SUPPORTED_IDS))
    driver._mgmt_bulk_out = 0x02
    return driver


def build_rtl8188eus_dkms(dev: RecordingUsbDevice) -> Driver:
    transport = Rtl8188eusTransport(dev)
    transport._out_ep = 0x02
    return Rtl8188eusDkmsDriver(transport)


def build_rtl8192fu(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8192FUDriver.from_usb_device(dev, first_id(rtl8192fu.SUPPORTED_IDS))
    driver.priv.out_ep = [0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D]
    driver.priv.nr_out_eps = 6
    rtl8xxxu_config_endpoints_no_sie(driver.priv)
    rtl8xxxu_init_queue_priority(driver.priv)
    driver._started = True
    return driver


def build_rtl8812au(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8812AUDriver.from_usb_device(dev, first_id(rtl8812au.SUPPORTED_IDS))
    driver._bulk_out_eps = [0x02, 0x03, 0x04]
    return driver


def build_rtl8812au_dkms(dev: RecordingUsbDevice) -> Driver:
    driver = Rtl8812auDkmsDriver.from_usb_device(dev, first_id(rtl8812au_dkms.SUPPORTED_IDS))
    driver._channel = 1
    return driver


def build_rtl8814au_dkms(dev: RecordingUsbDevice) -> Driver:
    return Rtl8814auDkmsDriver.from_usb_device(dev, first_id(rtl8814au_dkms.SUPPORTED_IDS))


def build_rtl8821au(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8821AUDriver.from_usb_device(dev, first_id(rtl8821au.SUPPORTED_IDS))
    driver._bulk_out_eps = [0x05, 0x06, 0x08, 0x09]
    return driver


def build_rtl8821au_dkms(dev: RecordingUsbDevice) -> Driver:
    driver = Rtl8821auDkmsDriver.from_usb_device(dev, first_id(rtl8821au_dkms.SUPPORTED_IDS))
    driver._channel = 1
    return driver


def build_rtl8821cu_dkms(dev: RecordingUsbDevice) -> Driver:
    return Rtl8821cuDkmsDriver.from_usb_device(dev, first_id(rtl8821cu_dkms.SUPPORTED_IDS))


def build_rtl8822bu(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8822BUDriver.from_usb_device(dev, first_id(rtl8822bu.SUPPORTED_IDS))
    driver._bulk_out_eps = [0x05, 0x06, 0x08]
    return driver


def build_rtl8822bu_dkms(dev: RecordingUsbDevice) -> Driver:
    return Rtl8822buDkmsDriver.from_usb_device(dev, first_id(rtl8822bu_dkms.SUPPORTED_IDS))


def build_rtl8822cu(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8822CUDriver.from_usb_device(dev, first_id(rtl8822cu.SUPPORTED_IDS))
    driver._bulk_out_eps = [0x05, 0x06, 0x08]
    return driver


def build_rtl8922au(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8922AUDriver.from_usb_device(dev, first_id(rtl8922au.SUPPORTED_IDS))
    driver._mgmt_ep = 0x05
    return driver


def build_rtw88_8814au(dev: RecordingUsbDevice) -> Driver:
    driver = RTL8814AUDriver.from_usb_device(dev, first_id(rtw88_8814au.SUPPORTED_IDS))
    driver._bulk_out_eps = [0x02]
    return driver


REALTEK_BMC = DescriptorBit(0, 1 << 24)               # W0[24] BMC: HW skips the ACK wait
REALTEK_W8_EN_HWSEQ = DescriptorBit(32, 1 << 15)      # W8[15] EN_HWSEQ

DRIVER_CASES: dict[str, DriverCase] = {
    # HTC: HIF 4 + htc_frame_hdr 8 + tx_mgmt_hdr 8 (tx_frame_hdr 12 for data); FW decides ACK.
    "ar9271_v2": DriverCase(build_ar9271_v2, 20, 24, hw_seq_bit=None, seq_stamped_by_firmware=True),
    "mt7601u": DriverCase(build_mt7601u, 24, 24, hw_seq_bit=None,
                          ack_request_bit=DescriptorBit(8, MT7601U_ACK_CTL_REQ)),
    "mt76x0u": DriverCase(build_mt76x0u, 24, 24, hw_seq_bit=None,
                          ack_request_bit=DescriptorBit(8, MT76X0U_ACK_CTL_REQ)),
    "mt76x2u": DriverCase(build_mt76x2u, 24, 24, hw_seq_bit=None,
                          ack_request_bit=DescriptorBit(8, MT76X2U_ACK_CTL_REQ)),
    "mt7921au": DriverCase(build_mt7921au, 68, 68, hw_seq_bit=None,
                           no_ack_bit=DescriptorBit(16, MT7921AU_TXD3_NO_ACK)),
    "mt7925au": DriverCase(build_mt7925au, 68, 68, hw_seq_bit=None,
                           no_ack_bit=DescriptorBit(16, MT7925AU_TXD3_NO_ACK)),
    # rt2500usb never requests an ACK, on purpose (docs/ACKS.md).
    "rt2500usb": DriverCase(build_rt2500usb, 20, 20,
                            hw_seq_bit=DescriptorBit(0, RT2500USB_TXD_W0_NEW_SEQ)),
    "rt2800usb": DriverCase(build_rt2800usb, 20, 20, hw_seq_bit=None,
                            ack_request_bit=DescriptorBit(8, RT2800USB_TXWI_W1_ACK)),
    "rt3070": DriverCase(build_rt3070, 20, 20, hw_seq_bit=None,
                         ack_request_bit=DescriptorBit(8, RT3070_TXWI_W1_ACK)),
    "rt5370": DriverCase(build_rt5370, 20, 20, hw_seq_bit=None,
                         ack_request_bit=DescriptorBit(8, RT5370_TXWI_W1_ACK)),
    "rt5372": DriverCase(build_rt5372, 20, 20, hw_seq_bit=None,
                         ack_request_bit=DescriptorBit(8, RT5372_TXWI_W1_ACK)),
    "rt5572": DriverCase(build_rt5572, 24, 24, hw_seq_bit=None,
                         ack_request_bit=DescriptorBit(8, RT5572_TXWI_W1_ACK)),
    # The 8187L tx_hdr has no no-ACK flag; the MAC decides from addr1.
    "rtl8187": DriverCase(build_rtl8187, 12, 12, hw_seq_bit=None),
    "rtl8188eus": DriverCase(build_rtl8188eus, 32, 32, hw_seq_bit=None,
                             no_ack_bit=DescriptorBit(0, RTL8188EUS_BROADMULTICAST << 24)),
    "rtl8188eus_dkms": DriverCase(build_rtl8188eus_dkms, 32, 32,
                                  hw_seq_bit=DescriptorBit(12, 1 << 31),    # txdw3 EN_HWSEQ
                                  no_ack_bit=DescriptorBit(0, RTL8188EUS_DKMS_BMC)),
    "rtl8192fu": DriverCase(build_rtl8192fu, 40, 40, hw_seq_bit=None,
                             no_ack_bit=DescriptorBit(0, RTL8192FU_CONSTANTS.TXDESC_BROADMULTICAST << 24)),
    "rtl8812au": DriverCase(build_rtl8812au, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8812au_dkms": DriverCase(build_rtl8812au_dkms, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8814au_dkms": DriverCase(build_rtl8814au_dkms, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8821au": DriverCase(build_rtl8821au, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8821au_dkms": DriverCase(build_rtl8821au_dkms, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8821cu_dkms": DriverCase(build_rtl8821cu_dkms, 48, 48, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8822bu": DriverCase(build_rtl8822bu, 48, 48, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8822bu_dkms": DriverCase(build_rtl8822bu_dkms, 48, 48, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
    "rtl8822cu": DriverCase(build_rtl8822cu, 48, 48, hw_seq_bit=None, no_ack_bit=REALTEK_BMC),
    "rtl8922au": DriverCase(build_rtl8922au, 64, 64, hw_seq_bit=None,
                            no_ack_bit=DescriptorBit(24, 1 << 12)),    # BODY6 NO_ACK
    "rtw88_8814au": DriverCase(build_rtw88_8814au, 40, 40, REALTEK_W8_EN_HWSEQ, REALTEK_BMC),
}

NULL_DATA_TO_AP = (b"\x48\x01\x00\x00" + AP_MAC + STATION_MAC + AP_MAC + b"\x00\x00")

FRAMES: dict[str, bytes] = {
    "unicast_deauth": build_deauth(STATION_MAC, AP_MAC, AP_MAC, 7),
    "broadcast_deauth": build_deauth(BROADCAST_MAC, AP_MAC, AP_MAC, 7),
    "auth": auth_req(AP_MAC, STATION_MAC),
    "broadcast_probe_request": probe_req(BROADCAST_MAC, STATION_MAC, ""),
    "unicast_null_data": NULL_DATA_TO_AP,
}


def driver_package_names() -> list[str]:
    chips_dir = Path(chips.__file__).parent
    return sorted(init.parent.name for init in chips_dir.glob("*/__init__.py")
                  if "def import_driver" in init.read_text(encoding="utf-8"))


def without_seq_ctrl(frame: bytes) -> bytes:
    return frame[:22] + b"\x00\x00" + frame[24:]


def seq_number(frame: bytes) -> int:
    return int.from_bytes(frame[22:24], "little") >> 4


async def inject_and_capture(driver: Driver, dev: RecordingUsbDevice, frame: bytes) -> bytes:
    sent_before = len(dev.bulk_writes)
    assert await driver.inject_frame(frame) is True
    payloads = [w for w in dev.bulk_writes[sent_before:] if w]    # drop rtl8187's ZLP
    assert len(payloads) == 1, f"expected one bulk-OUT write, got {len(payloads)}"
    return payloads[0]


@pytest.mark.parametrize("frame_name", list(FRAMES))
@pytest.mark.parametrize("package_name", driver_package_names())
async def test_inject_frame_wire_bytes(package_name: str, frame_name: str) -> None:
    case = DRIVER_CASES[package_name]
    frame = FRAMES[frame_name]
    dev = RecordingUsbDevice()
    driver = case.build(dev)
    is_data = (frame[0] & 0x0C) == 0x08
    offset = case.data_payload_offset if is_data else case.payload_offset

    first_payload = await inject_and_capture(driver, dev, frame)
    second_payload = await inject_and_capture(driver, dev, frame)
    first_mpdu = first_payload[offset:offset + len(frame)]
    second_mpdu = second_payload[offset:offset + len(frame)]

    assert without_seq_ctrl(first_mpdu) == without_seq_ctrl(frame), "802.11 bytes altered"

    if case.hw_seq_bit is not None:
        assert case.hw_seq_bit.is_set(first_payload), "HW-seq descriptor flag clear"
    elif not case.seq_stamped_by_firmware:
        assert seq_number(second_mpdu) == (seq_number(first_mpdu) + 1) & 0xFFF, (
            f"no HW-seq flag and seq not stamped: {seq_number(first_mpdu)} then "
            f"{seq_number(second_mpdu)}")

    is_group_addr1 = bool(frame[4] & 0x01)
    if case.no_ack_bit is not None:
        assert case.no_ack_bit.is_set(first_payload) == is_group_addr1, (
            f"no-ACK bit {'clear' if is_group_addr1 else 'set'} for "
            f"{'group' if is_group_addr1 else 'unicast'} addr1")
    if case.ack_request_bit is not None:
        assert case.ack_request_bit.is_set(first_payload) != is_group_addr1, (
            f"ACK-request bit {'set' if is_group_addr1 else 'clear'} for "
            f"{'group' if is_group_addr1 else 'unicast'} addr1")
