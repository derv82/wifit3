from pathlib import Path
from unittest.mock import MagicMock, patch

from wifit3.device.manager import _pci_netdev_devices, devices, supported_ids, wlan_iface
from wifit3.models.device_id import DeviceID
from wifit3.setup.linux import SetupLinux


def test_pci_netdev_devices_enumerates_matching_cards(tmp_path: Path):
    net_dir = tmp_path / "sys_class_net"
    wlan_dir = net_dir / "wlp62s0"
    (wlan_dir / "wireless").mkdir(parents=True)
    dev_dir = wlan_dir / "device"
    dev_dir.mkdir(parents=True)
    (dev_dir / "vendor").write_text("0x14c3\n")
    (dev_dir / "device").write_text("0x7922\n")

    # Mock subsystem symlink pointing to /sys/bus/pci
    subsys_target = tmp_path / "pci"
    subsys_target.mkdir()
    (dev_dir / "subsystem").symlink_to(subsys_target)

    smap = supported_ids()
    found = _pci_netdev_devices(smap, sysfs_net=net_dir)

    assert len(found) == 1
    dev = found[0]
    assert dev.chipset == "MT7922"
    assert dev.vid == 0x14C3
    assert dev.pid == 0x7922
    assert dev.bus == "pci"
    assert dev.extras["iface"] == "wlp62s0"
    assert dev.extras["bus_type"] == "pci"


def test_wlan_iface_constructs_netdev_driver():
    dev_id = DeviceID(
        vid=0x14C3,
        pid=0x7922,
        chipset="MT7922",
        vendor="MediaTek",
        product_name="MT7922 802.11ax PCIe (wlp62s0)",
        bus="pci",
        address="0000:3e:00.0",
        extras={"iface": "wlp62s0", "bus_type": "pci"},
    )
    iface = wlan_iface(dev_id, name="wlan_pci0")
    assert iface is not None
    assert iface.name == "wlan_pci0"
    assert iface.chipset == "MT7922"
    assert iface.bus == "pci"
    from wifit3.chips.netdev.driver import LinuxNetdevDriver
    assert isinstance(iface.driver, LinuxNetdevDriver)


def test_pci_card_does_not_require_setup():
    setup = SetupLinux()
    dev_id = DeviceID(
        vid=0x14C3,
        pid=0x7922,
        chipset="MT7922",
        vendor="MediaTek",
        bus="pci",
        address="0000:3e:00.0",
        extras={"iface": "wlp62s0", "bus_type": "pci"},
    )
    assert setup.requires_setup(dev_id) is False


def test_devices_includes_both_usb_and_pci():
    mock_usb_dev = DeviceID(
        vid=0x0E8D,
        pid=0x7961,
        chipset="MT7921AU",
        vendor="MediaTek",
        bus=1,
        address=2,
    )
    mock_pci_dev = DeviceID(
        vid=0x14C3,
        pid=0x7922,
        chipset="MT7922",
        vendor="MediaTek",
        bus="pci",
        address="0000:3e:00.0",
        extras={"iface": "wlp62s0", "bus_type": "pci"},
    )
    with patch("wifit3.device.manager._bus_devices", return_value=[]):
        with patch("wifit3.device.manager._pnp_only_devices", return_value=[mock_usb_dev]):
            with patch("wifit3.device.manager._pci_netdev_devices", return_value=[mock_pci_dev]):
                all_devs = devices()
                assert mock_usb_dev in all_devs
                assert mock_pci_dev in all_devs
