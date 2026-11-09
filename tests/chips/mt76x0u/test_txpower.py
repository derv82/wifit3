"""mt76x0_phy_set_txpower against the Linux driver's writes for one card's EEPROM (AWUS036ACHM)."""
from wifit3.chips.mt76x0u.eeprom import EEPROMCache
from wifit3.chips.mt76x0u.phy import mt76x0_chan_max_power, mt76x0_phy_set_txpower

# The card's TX power EEPROM words only; everything else unburned.
_AWUS_POWER_BYTES = {
    0x036: "0810",
    0x050: "ff800505050606060707070707070707ffffffffffffffffffffffffffffffffffffffffff240000"
           "1919191919191919191919191212111111111010101010111111111212121213131314ffffffffff",
    0x0d0: "02fb1a00407495ffffffff00649b00003d3d003c3d3d003c3d3d003c3d3d003c",
    0x120: "0707040007070400070704003f3effff",
}
_ORDER = [0x1314, 0x1318, 0x131c, 0x1320, 0x1324, 0x13d4, 0x13d8, 0x13dc]


class _RecordingTransport:
    def __init__(self, alc_cfg_0: int):
        self.regs = {0x13b0: alc_cfg_0}

    def read32(self, addr: int) -> int:
        return self.regs.get(addr, 0)

    def write32(self, addr: int, val: int) -> None:
        self.regs[addr] = val


def _awus_cache() -> EEPROMCache:
    data = bytearray(b"\xff" * 512)
    for offset, hex_bytes in _AWUS_POWER_BYTES.items():
        raw = bytes.fromhex(hex_bytes)
        data[offset:offset + len(raw)] = raw
    return EEPROMCache(bytes(data))


def _set_txpower(channel: int) -> _RecordingTransport:
    cache = _awus_cache()
    transport = _RecordingTransport(alc_cfg_0=0x2F2F000C)
    mt76x0_phy_set_txpower(transport, cache, channel, mt76x0_chan_max_power(cache, channel) * 2)
    return transport


def test_ch3_matches_linux_capture():
    transport = _set_txpower(3)
    assert transport.regs[0x13b0] == 0x2F2F0505
    assert [transport.regs[a] for a in _ORDER] == [
        0x3D3D3F3F, 0x3D3D3C3F, 0x3F3F3C3F, 0x3D3D3F3F, 0x00003C3F, 0x3E3C3F3C, 0x3E3F003F, 0x3E3F003C]


def test_ch40_matches_linux_capture():
    transport = _set_txpower(40)
    assert transport.regs[0x13b0] == 0x2F2F1919
    assert [transport.regs[a] for a in _ORDER] == [
        0x07070000, 0x07070004, 0x00000004, 0x07070000, 0x00000004, 0x3E003F00, 0x3E3F0000, 0x3E3F0000]


def test_ch3_cap_matches_linux_iw_txpower():
    # `iw dev` under the Linux driver on this card, ch 3: "txpower 2.00 dBm".
    assert mt76x0_chan_max_power(_awus_cache(), 3) == 2
