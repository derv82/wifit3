"""rtl8192fu core.c port: filter, queue priority, EFUSE decode, CFO watchdog."""
import pytest

from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.core import (
    MONITOR_FILTER_FLAGS,
    RF_A,
    rtl8xxxu_config_endpoints_no_sie,
    rtl8xxxu_configure_filter,
    rtl8xxxu_init_mac,
    rtl8xxxu_init_phy_regs,
    rtl8xxxu_init_queue_priority,
    rtl8xxxu_init_rf_regs,
    rtl8xxxu_read_efuse,
    rtl8xxxu_watchdog_callback,
)

from .fake import make_priv

OUT_EP = [0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D]


def test_configure_filter_monitor_flags_match_capture():
    priv, transport = make_priv()
    priv.regrcr = 0x7000600E
    rtl8xxxu_configure_filter(priv, MONITOR_FILTER_FLAGS)
    assert transport.writes() == [(C.REG_RCR, 4, 0x7000702F)]
    assert priv.regrcr == 0x7000702F


def test_init_queue_priority_six_endpoints():
    priv, transport = make_priv({C.REG_TRXDMA_CTRL: 0xFFFFFFFF})
    priv.out_ep, priv.nr_out_eps = OUT_EP, 6
    rtl8xxxu_config_endpoints_no_sie(priv)
    rtl8xxxu_init_queue_priority(priv)

    assert priv.ep_tx_count == 3
    expected = 0x7 | 3 << 4 | 2 << 7 | 1 << 10 | 1 << 13 | 3 << 16 | 3 << 19
    assert transport.writes() == [(C.REG_TRXDMA_CTRL, 4, expected)]
    # vop = viq ^ 3 (core.c:2661): VO shares VI's endpoint.
    assert priv.pipe_out[C.TXDESC_QUEUE_VO] == priv.pipe_out[C.TXDESC_QUEUE_VI] == 0x06
    assert priv.pipe_out[C.TXDESC_QUEUE_BE] == priv.pipe_out[C.TXDESC_QUEUE_BK] == 0x08
    assert priv.pipe_out[C.TXDESC_QUEUE_MGNT] == priv.pipe_out[C.TXDESC_QUEUE_HIGH] == 0x05


def test_read_efuse_decodes_headers_and_word_mask():
    physical = bytes([
        0x1E, 0xAA, 0xBB,             # section 1, word_mask 0xE: only word 0 -> map 0x08..0x09
        0x2F, 0x2E, 0x11, 0x22,       # ext header: section (0x2F>>5)|(0x2E&0xF0)>>1 = 0x11, word 0
        0x0F, 0xFF,                   # ext header with all words disabled: skipped
        0xFF,                         # end
    ])
    priv, transport = make_priv()
    transport.read_hooks[C.REG_EFUSE_CTRL] = lambda: (
        C.BIT(31) | physical[transport.regs.get(C.REG_EFUSE_CTRL + 1, 0)])
    rtl8xxxu_read_efuse(priv)

    assert priv.efuse_wifi[0x08:0x0A] == b"\xaa\xbb"
    assert priv.efuse_wifi[0x88:0x8A] == b"\x11\x22"
    assert priv.efuse_wifi.count(0xFF) == C.EFUSE_MAP_LEN - 4
    assert transport.writes()[-1] == (C.REG_EFUSE_ACCESS, 1, C.EFUSE_ACCESS_DISABLE)


def test_watchdog_is_wire_silent_at_default_crystal_cap():
    priv, transport = make_priv()
    priv.default_crystal_cap = priv.cfo_tracking.crystal_cap = 0x25
    priv.cfo_tracking.atc_status = True
    rtl8xxxu_watchdog_callback(priv)
    assert transport.ops == []


def test_watchdog_steps_crystal_cap_toward_default():
    priv, transport = make_priv()
    priv.default_crystal_cap = 0x25
    priv.cfo_tracking.crystal_cap = 0x27
    priv.cfo_tracking.atc_status = False
    rtl8xxxu_watchdog_callback(priv)

    assert priv.cfo_tracking.crystal_cap == 0x26
    assert priv.cfo_tracking.atc_status
    written = [address for address, _, _ in transport.writes()]
    assert written == [C.REG_AFE_PLL_CTRL, C.REG_AFE_XTAL_CTRL, C.REG_OFDM1_CFO_TRACKING]


def test_init_mac_warns_and_fails_on_a_failed_write(caplog):
    priv, transport = make_priv()
    transport.write8 = lambda address, value: None
    with pytest.raises(OSError):
        rtl8xxxu_init_mac(priv)
    assert "Failed to initialize MAC (reg: " in caplog.text


def test_init_phy_regs_warns_and_stops_the_table_at_a_failed_write(caplog):
    priv, transport = make_priv()
    written = []

    def write32(address, value):
        written.append(address)
        return None if address == 0x804 else 4
    transport.write32 = write32
    rtl8xxxu_init_phy_regs(priv, ((0x800, 1), (0x804, 2), (0x808, 3), (0xFFFF, 0xFFFFFFFF)))
    assert written == [0x800, 0x804]
    assert "Failed to initialize PHY" in caplog.text


def test_init_rf_regs_warns_and_stops_the_table_at_a_failed_write(caplog):
    priv, transport = make_priv()
    written = []

    def write32(address, value):
        written.append(address)
        return None
    transport.write32 = write32
    rtl8xxxu_init_rf_regs(priv, ((0x00, 1), (0x01, 2), (0xFF, 0xFFFFFFFF)), RF_A)
    assert len(written) == 1
    assert "Failed to initialize RF" in caplog.text
