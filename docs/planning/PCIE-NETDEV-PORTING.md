# PCIe / M.2 Support & Linux Netdev Backend (Planning Doc)

This document specifies the architectural plan for extending `wifit3` from USB-only adapters to PCIe and M.2 wireless adapters (e.g., MediaTek MT7922), fulfilling Objectives 1 and 3 without regressing existing USB dongle support.

---

## 1. Context & Architectural Reality

`wifit3` is historically a pure-userspace Python 802.11 stack communicating directly with USB dongles via `PyUSB` (`libusb`). In that model:
- The Linux kernel driver is blacklisted (`/etc/modprobe.d/wifit3-*.conf`).
- Udev rules (`/etc/udev/rules.d/60-wifit3-*.rules`) grant non-root users access to the raw usbfs device node (`/dev/bus/usb/BBB/DDD`).
- Hardware init, firmware loading, register configuration, channel tuning, and packet parsing are all handled in Python userland.

PCIe / M.2 adapters (such as the MediaTek MT7922 `14c3:7922` driven by `mt7921e`) communicate via PCIe MMIO and coherent host-memory DMA descriptor rings, not USB endpoints. Rather than writing a bare-metal userspace PCIe DMA engine in Python for every PCIe chipset, `wifit3` can support PCIe/M.2 cards via **Path A**: a hybrid `LinuxNetdevDriver` backed by the Linux kernel's standard netdev and `nl80211` interfaces.

---

## 2. Path A Design Specification

### 2.1 Device Enumeration (`src/wifit3/device/manager.py`)
- Query `/sys/class/net/*/wireless` or `/sys/class/net/*/phy80211` to identify wireless network interfaces.
- Resolve the underlying PCI vendor and device IDs via `/sys/class/net/<iface>/device/vendor` and `device/device`.
- When matching a supported PCIe chipset (e.g. `0x14c3:0x7922` MT7922):
  - Instantiate a `DeviceID` with `bus="pci"`, `vid=0x14c3`, `pid=0x7922`, `chipset="MT7922"`, `extras={"iface": iface, "bus_type": "pci"}`.
  - Expose this device in `DeviceManager.devices()` alongside standard USB devices.

### 2.2 Driver Implementation (`src/wifit3/chips/netdev/`)
- Subclass `wifit3.chips.driver.Driver`.
- **Bring-up & Monitor Mode**:
  - Bring down managed mode or add a virtual monitor interface (`iw dev <iface> interface add <mon_iface> type monitor`).
  - Open a raw `AF_PACKET` socket (`socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))`) bound to the monitor interface.
- **RX Path**:
  - Register the raw socket with the asyncio event loop (`loop.add_reader`).
  - Strip the radiotap header, extract RSSI, and pass 802.11 MPDUs to `WlanFrameParser.parse(mpdu, rssi=rssi)`.
  - Dispatch parsed `Packet` objects to the registered RX callback.
- **Channel Tuning**:
  - Invoke `iw dev <mon_iface> set channel <channel>` asynchronously.
- **TX Frame Injection**:
  - Prepend a standard minimal radiotap header (8 bytes) and write the raw 802.11 frame directly to the `AF_PACKET` socket.

### 2.3 Hot-plug & Watch (`src/wifit3/device/watch.py`)
- Update `_diff()` to differentiate PCI netdevs by `instance_key = (d.vid, d.pid, d.bus, d.address or d.extras.get("iface"))`.

---

## 3. Operational Safety on Host Machine

On the current development host:
- `wlp62s0` is the active network interface carrying the default route (`10.0.1.15` via `10.0.1.138`).
- The running AI agent session relies on `wlp62s0` to contact Google Cloud APIs.
- The `mt7921e` driver does not support simultaneous managed and monitor modes on the same phy.
- **Pre-requisite for testing monitor mode on `wlp62s0`**:
  - Either establish an alternative internet uplink (e.g. Ethernet on `enp63s0`), OR
  - Perform live testing on the secondary MT7922 adapter `wlp64s0` (`phy1`), which is not in use by the OS routing table.
