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

    def write_block(self, addr: int, data: bytes) -> int:
        return len(data)

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


class ShortBlockTransport(FakeTransport):
    """write_block fails (usb_control_msg error) for the first ``failures`` blocks."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.failed_blocks = 0
        self.written_blocks = 0

    def write_block(self, addr: int, data: bytes) -> int | None:
        if self.failed_blocks < self.failures:
            self.failed_blocks += 1
            return None
        self.written_blocks += 1
        return len(data)


def _probe(monkeypatch, transport) -> None:
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "rtl8xxxu_read_efuse", _valid_efuse)
    priv = Rtl8xxxuPriv(transport, rtl8192eu_fops)
    priv.out_ep = [0x02, 0x03, 0x05]
    priv.nr_out_eps = 3
    fw = files("wifit3.chips.rtl8192eu").joinpath("assets/rtl8192eu_nic.bin").read_bytes()
    rtl8xxxu_probe(priv, fw)


def test_firmware_download_retries_after_a_short_block(monkeypatch):
    # core.c:3962-3970: download_firmware returns -EAGAIN; init_device retries.
    transport = ShortBlockTransport(failures=5)
    _probe(monkeypatch, transport)
    fw_size = len(files("wifit3.chips.rtl8192eu").joinpath("assets/rtl8192eu_nic.bin").read_bytes()) - 32
    assert transport.written_blocks == -(-fw_size // rtl8192eu_fops.writeN_block_size)   # one whole image


def test_firmware_download_gives_up_after_six_attempts(monkeypatch):
    transport = ShortBlockTransport(failures=100)
    with pytest.raises(IOError, match="firmware download failed"):
        _probe(monkeypatch, transport)
    assert transport.failed_blocks == 6


class MacTableWriteFails(FakeTransport):
    def write8(self, addr: int, val: int) -> int | None:
        super().write8(addr, val)
        return None if (addr, val) == rtl8192eu_fops.mactable[0] else 1


def test_init_mac_aborts_on_a_failed_write(monkeypatch):
    # core.c:2201-2206: init_mac returns -EAGAIN; init_device aborts.
    with pytest.raises(IOError, match="Failed to initialize MAC"):
        _probe(monkeypatch, MacTableWriteFails())


class Bonding1T2RTransport(FakeTransport):
    def read32(self, addr: int) -> int:
        return C.HPON_FSM_BONDING_1T2R if addr == C.REG_HPON_FSM else super().read32(addr)


def test_8191eu_takes_the_non_8192e_branches(monkeypatch):
    # 8192e.c:481-484 identifies 1T2R bonding as RTL8191E; core.c tests rtl_chip == RTL8192E.
    transport = Bonding1T2RTransport()
    _probe(monkeypatch, transport)
    written = {addr for addr, _val in transport.writes}
    assert (C.REG_FPGA0_TX_INFO, 0x00000003) in transport.writes          # core.c:4002
    assert (C.REG_FPGA0_XA_RF_INT_OE, 0x66f60210) in transport.writes     # core.c:4017
    assert (C.REG_PBP, 0x00) in transport.writes                          # core.c:4040
    assert (C.REG_HIMR, 0xffffffff) in transport.writes                   # core.c:4124
    assert (C.REG_FPGA0_POWER_SAVE, 0x20000) not in transport.writes      # core.c:940-944
    assert not written & {C.REG_HIMR0, C.REG_AFE_XTAL_CTRL, C.REG_USB_HRPWM, C.REG_ACLK_MON, C.REG_AFE_MISC}
