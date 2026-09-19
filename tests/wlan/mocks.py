from wifit3.chips.driver import FakeMacSupport
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray
from wifit3.wlan.interface import WlanInterface

class MockDriver:
    """A dummy driver that swallows TX and pretends to tune channels."""

    FAKE_MAC = FakeMacSupport.SPOOFABLE
    SUPPORTED_CHANNELS = list(range(1, 15))

    def __init__(self, mac_address: bytes = b"\x11\x22\x33\x44\x55\x66"):
        self.mac_address = mac_address

    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass


def mock_array(cards: int = 1) -> WlanArray:
    """Returns a real WlanArray populated with real WlanInterfaces."""
    array = WlanArray()
    for i in range(cards):
        driver = MockDriver(mac_address=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x60 + i]))
        iface = WlanInterface(driver, name=f"wlan{i}", description=f"Mock card {i}", chipset=f"MockChipset{i}")
        array.attach(iface)

    if cards > 0:
        array._preferred = array._members[0]

    return array


def build_ap(bssid: str = "aa:bb:cc:dd:ee:01", 
             ssid: str = "TESTNET", 
             encryption: str = "WPA2", 
             wpa3: bool = False,
             **kwargs) -> AccessPoint:
    """Returns a real AP seeded with defaults for testing."""

    # Map common encryption strings to realistic AKMs so UI derives state correctly
    if "akms" not in kwargs:
        if encryption == "WPA2":
            kwargs["akms"] = ["PSK"]
            kwargs["akm_suites"] = [2]  # _AKM_NUM["PSK"] == 2
            kwargs.setdefault("pairwise_cipher", "CCMP")
        elif encryption in ("WEP", "OPEN"):
            kwargs["akms"] = []
            kwargs["akm_suites"] = []

    # Provide a dummy beacon frame so it doesn't crash code that parses it
    kwargs.setdefault("last_beacon_frame", b"\x80\x00beacon")

    return AccessPoint(
        bssid=bssid,
        ssid=ssid,
        encryption=encryption,
        wpa3=wpa3,
        **kwargs
    )
