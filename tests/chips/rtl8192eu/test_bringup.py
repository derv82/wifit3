"""Offline bring-up smoke test: run the whole rtl8xxxu_probe + monitor register sequence
against a mock transport (no hardware). Proves the orchestration executes end-to-end."""
from importlib.resources import files

import pytest

from wifit3.chips.rtl8192eu import constants as C
from wifit3.chips.rtl8192eu import core
from wifit3.chips.rtl8192eu.core import Rtl8xxxuPriv, rtl8xxxu_probe, start_monitor_interface
from wifit3.chips.rtl8192eu.rtl8192e import rtl8192eu_fops

# read32 values that let every bring-up poll complete:
_READ32 = {
    C.REG_APS_FSMCO: C.BIT(17),                               # power ready; MAC_ENABLE clears
    C.REG_MCU_FW_DL: C.MCU_FW_DL_CSUM_REPORT | C.MCU_WINT_INIT_READY,  # fw checksum + init ready
    C.REG_AUTO_LLT: 0,                                        # LLT init done
}


class FakeTransport:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def read8(self, addr: int) -> int:
        return (_READ32.get(addr, 0)) & 0xff

    def read16(self, addr: int) -> int:
        return (_READ32.get(addr, 0)) & 0xffff

    def read32(self, addr: int) -> int:
        return _READ32.get(addr, 0)

    def write8(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 1

    def write16(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 2

    def write32(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 4

    def write_block(self, addr: int, data: bytes) -> None:
        return

    def bulk_write(self, endpoint: int, data: bytes) -> None:
        return


def _valid_efuse(priv: Rtl8xxxuPriv) -> None:
    efuse = bytearray(b"\xff" * C.EFUSE_MAP_LEN)
    efuse[0x00:0x02] = (0x8129).to_bytes(2, "little")
    efuse[0xd7:0xdd] = bytes.fromhex("0011223344ee")
    efuse[0xb9] = 0x20
    priv.efuse_wifi = efuse


def test_bringup_runs_end_to_end(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)   # skip all the udelay/mdelay waits
    monkeypatch.setattr(core, "rtl8xxxu_read_efuse", _valid_efuse)  # inject a valid efuse map

    transport = FakeTransport()
    priv = Rtl8xxxuPriv(transport, rtl8192eu_fops)
    priv.out_ep = [0x02, 0x03, 0x05]   # what rtl8xxxu_parse_usb would populate
    priv.nr_out_eps = 3
    priv.pipe_in = 0x81

    fw = files("wifit3.chips.rtl8192eu").joinpath("assets/rtl8192eu_nic.bin").read_bytes()
    rtl8xxxu_probe(priv, fw)
    start_monitor_interface(priv)

    assert priv.mac_addr == bytes.fromhex("0011223344ee")
    assert priv.fw_size == len(fw) - 32
    # RCR was programmed and the monitor filter applied (FIF_CONTROL keeps RCR_ACCEPT_CTRL_FRAME set).
    rcr_writes = [val for (addr, val) in transport.writes if addr == C.REG_RCR]
    assert rcr_writes, "REG_RCR was never written"
    assert priv.regrcr & C.RCR_ACCEPT_CTRL_FRAME
    # pipe_out was mapped for every TX queue, so inject_frame can resolve an endpoint.
    assert priv.pipe_out[C.TXDESC_QUEUE_MGNT] in priv.out_ep


def test_bringup_rejects_blank_efuse(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "rtl8xxxu_read_efuse", lambda priv: None)  # leaves 0xff map

    priv = Rtl8xxxuPriv(FakeTransport(), rtl8192eu_fops)
    priv.out_ep = [0x02, 0x03, 0x05]
    priv.nr_out_eps = 3
    fw = files("wifit3.chips.rtl8192eu").joinpath("assets/rtl8192eu_nic.bin").read_bytes()
    with pytest.raises(ValueError):   # parse_efuse rejects the blank rtl_id
        rtl8xxxu_probe(priv, fw)
