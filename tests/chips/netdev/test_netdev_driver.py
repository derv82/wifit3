import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from wifit3.chips.netdev.driver import LinuxNetdevDriver, parse_radiotap
from wifit3.models.device_id import DeviceID


def _build_radiotap(mpdu: bytes, rssi: int = -42, has_fcs: bool = False) -> bytes:
    # Radiotap with TSFT (8B) and DBM_ANTSIGNAL (1B), optionally FLAGS (1B)
    flags = 0x10 if has_fcs else 0x00
    it_present = (1 << 0) | (1 << 1) | (1 << 5)  # TSFT + FLAGS + DBM_ANTSIGNAL
    # Header: version(1), pad(1), len(2), present(4) -> 8 bytes
    # TSFT: 8B aligned at 8 -> 8 bytes
    # FLAGS: 1B -> 1 byte
    # DBM_ANTSIGNAL: 1B -> 1 byte
    # Total len = 18 bytes
    it_len = 18
    hdr = struct.pack("<BBHI", 0, 0, it_len, it_present)
    tsft = b"\x00" * 8
    fields = bytes([flags, rssi & 0xFF])
    payload = mpdu + (b"\x12\x34\x56\x78" if has_fcs else b"")
    return hdr + tsft + fields + payload


def test_parse_radiotap_extracts_mpdu_and_rssi():
    mpdu = b"\x80\x00" + b"\x00" * 22
    data = _build_radiotap(mpdu, rssi=-55, has_fcs=False)
    extracted_mpdu, rssi = parse_radiotap(data)
    assert extracted_mpdu == mpdu
    assert rssi == -55


def test_parse_radiotap_strips_fcs_when_flag_set():
    mpdu = b"\x80\x00" + b"\x00" * 22
    data = _build_radiotap(mpdu, rssi=-60, has_fcs=True)
    extracted_mpdu, rssi = parse_radiotap(data)
    assert extracted_mpdu == mpdu
    assert rssi == -60


def test_parse_radiotap_handles_short_data():
    mpdu, rssi = parse_radiotap(b"\x00\x00\x04")
    assert mpdu == b"\x00\x00\x04"
    assert rssi == -50


def test_parse_radiotap_reads_signal_from_first_present_word():
    # Real mac80211 header captured from an MT7922 (wlp62s0): three present words
    # (ext-present chain), TSFT+flags+rate+channel+dBm-antsignal, antsignal byte 0xc5 = -59.
    header = bytes.fromhex(
        "000038002f4040a0200800a020080000"
        "46b217000000000000028509a000c500"
        "000000000000000046b217000000000000000101c300c201"
    )
    mpdu = b"\x80\x00" + b"\x00" * 22
    _, rssi = parse_radiotap(header + mpdu)
    assert rssi == -59, "field offsets must be decoded from the first present word, not the last"


@pytest.fixture
def mock_id():
    return DeviceID(
        vid=0x14C3,
        pid=0x7922,
        chipset="MT7922",
        vendor="MediaTek",
        product_name="MT7922 802.11ax PCIe",
        bus="pci",
        address="0000:3e:00.0",
        extras={"iface": "wlp62s0", "bus_type": "pci"},
    )


def test_driver_init_attributes(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    assert driver.iface == "wlp62s0"
    assert driver.id_entry == mock_id
    assert 1 in driver.SUPPORTED_CHANNELS
    assert 36 in driver.SUPPORTED_CHANNELS


async def test_driver_connect_when_already_monitor(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    with patch.object(driver, "_is_monitor", return_value=True):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            progress = MagicMock()
            mock_loop = MagicMock()
            with patch("asyncio.get_running_loop", return_value=mock_loop):
                ok = await driver.connect(progress_cb=progress)
                assert ok is True
                mock_sock.bind.assert_called_once_with(("wlp62s0", 0))
                mock_loop.add_reader.assert_called_once_with(mock_sock.fileno(), driver._on_socket_read)
                assert progress.call_count >= 1


async def test_driver_on_socket_read_dispatches_packet(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    driver._sock = MagicMock()
    # 24-byte beacon header: FC=0x8000, duration, DA, SA, BSSID, seq
    beacon_mpdu = (
        b"\x80\x00\x00\x00"
        + b"\xff\xff\xff\xff\xff\xff"
        + b"\x00\x11\x22\x33\x44\x55"
        + b"\x00\x11\x22\x33\x44\x55"
        + b"\x00\x00"
        + b"\x00" * 12  # timestamp, interval, cap
        + b"\x00\x04Test"  # SSID IE
    )
    raw_packet = _build_radiotap(beacon_mpdu, rssi=-45)
    driver._sock.recv.return_value = raw_packet

    received = []
    driver.register_rx_callback(received.append)
    driver._on_socket_read()

    assert len(received) == 1
    assert received[0].bssid == "00:11:22:33:44:55"
    assert received[0].rssi == -45
    assert received[0].ssid == "Test"


async def test_driver_set_channel(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    mock_proc = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        ok = await driver.set_channel(6)
        assert ok is True
        mock_exec.assert_called_once_with(
            "iw", "dev", "wlp62s0", "set", "channel", "6", stdout=-3, stderr=-3
        )


async def test_driver_inject_frame(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    driver._sock = MagicMock()
    frame = b"\xc0\x00" + b"\x00" * 22  # deauth frame
    ok = await driver._inject_frame(frame)
    assert ok is True
    driver._sock.send.assert_called_once()
    sent = driver._sock.send.call_args[0][0]
    assert sent.startswith(b"\x00\x00\x08\x00\x00\x00\x00\x00")
    assert sent.endswith(frame)


async def test_driver_close(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    driver._sock = MagicMock()
    driver._loop = MagicMock()
    await driver.close()
    assert driver._sock is None


async def test_driver_close_restores_networkmanager(mock_id):
    driver = LinuxNetdevDriver.from_netdev("wlp62s0", mock_id)
    driver._changed_mode = True
    driver._nm_unmanaged = True
    with patch("wifit3.chips.netdev.driver.subprocess.run") as run:
        await driver.close()
    restore_cmd = run.call_args[0][0]
    assert "nmcli dev set wlp62s0 managed yes" in restore_cmd
