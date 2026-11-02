"""Init-table and bring-up-sequence tests for the MT7601U port.

The generated tables are checked against the C for shape (entry counts, address
bases) and cross-checked against each other where the kernel computes the same value
two ways. The register sequence is pinned with a recording transport.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u import initvals_mac, initvals_phy
from wifit3.chips.mt7601u.eeprom import MT7601UEepromParams
from wifit3.chips.mt7601u.init import (
    BEACON_OFFSETS,
    MT_USB_AGGR_SIZE_LIMIT,
    MT_USB_AGGR_TIMEOUT,
    QUEUE_DRAIN_PASSES,
    RX_FILTER_MONITOR,
    USB_DMA_AGG_EN_MAX_PACKET,
    BringUpError,
    MT7601UInit,
)
from wifit3.chips.mt7601u.mcu import MT7601UMcu
from wifit3.chips.mt7601u.phy import MT7601UPhy


class RecordingTransport:
    """Records every op; KICK/BUSY read clear so the polls succeed first time."""

    def __init__(self, max_packet: int = 512) -> None:
        self.ops: list[tuple] = []
        self.payloads: list[bytes] = []
        self.in_max_packet = max_packet
        self._files: dict[int, dict[int, int]] = {C.MT_RF_CSR_CFG: {},
                                                   C.MT_BBP_CSR_CFG: {C.MT_BBP_REG_VERSION: 0x88}}
        self._sel: dict[int, int] = {C.MT_RF_CSR_CFG: 0, C.MT_BBP_CSR_CFG: 0}
        self._sent_seq = 1

    def rr(self, offset: int) -> int:
        self.ops.append(("read", offset))
        if offset == C.MT_RF_CSR_CFG:
            return self._csr(C.MT_RF_CSR_CFG, C.MT_RF_CSR_CFG_REG_ID, C.MT_RF_CSR_CFG_DATA)
        if offset == C.MT_BBP_CSR_CFG:
            return self._csr(C.MT_BBP_CSR_CFG, C.MT_BBP_CSR_CFG_REG_NUM, C.MT_BBP_CSR_CFG_VAL)
        return 0

    def _csr(self, addr: int, sel_mask: int, data_mask: int) -> int:
        return (C._field_prep(sel_mask, self._sel[addr])
                | C._field_prep(data_mask, self._files[addr].get(self._sel[addr], 0)))

    def wr(self, offset: int, val: int) -> None:
        self.ops.append(("wr", offset, val))
        if offset == C.MT_RF_CSR_CFG:
            self._sel[offset] = C._field_get(C.MT_RF_CSR_CFG_REG_ID, val)
            if val & C.MT_RF_CSR_CFG_WR:
                self._files[offset][self._sel[offset]] = C._field_get(C.MT_RF_CSR_CFG_DATA, val)
        elif offset == C.MT_BBP_CSR_CFG:
            self._sel[offset] = C._field_get(C.MT_BBP_CSR_CFG_REG_NUM, val)
            if val & C.MT_BBP_CSR_CFG_RW_MODE and not (val & C.MT_BBP_CSR_CFG_READ):
                self._files[offset][self._sel[offset]] = C._field_get(C.MT_BBP_CSR_CFG_VAL, val)

    def rmw(self, offset: int, mask: int, val: int) -> int:
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        self.wr(offset, val)
        return val

    def rmc(self, offset: int, mask: int, val: int) -> int:
        cur = self.rr(offset)
        val |= cur & ~mask & 0xFFFFFFFF
        if cur != val:
            self.wr(offset, val)
        return val

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        self.ops.append(("bulk", len(data)))
        self.payloads.append(data)
        self._sent_seq = C._field_get(C.MT_TXD_CMD_INFO_SEQ,
                                      int.from_bytes(data[:4], "little"))
        return len(data)

    bulk_out_inband_fw = bulk_out

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        return (C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, self._sent_seq)
                | C._field_prep(C.MT_RXD_CMD_INFO_EVT_TYPE, C.CMD_DONE)).to_bytes(4, "little")

    def writes_to(self, offset: int) -> list[int]:
        return [op[2] for op in self.ops if op[0] == "wr" and op[1] == offset]

    def pairs_of(self, message: int) -> list[tuple[int, int]]:
        payload = self.payloads[message]
        ln = C._field_get(C.MT_TXD_INFO_LEN, int.from_bytes(payload[:4], "little"))
        body = payload[4:4 + ln]
        return [(int.from_bytes(body[j:j + 4], "little"),
                 int.from_bytes(body[j + 4:j + 8], "little"))
                for j in range(0, len(body) - 7, 8)]


def make_init(max_packet: int = 512) -> tuple[MT7601UInit, RecordingTransport]:
    tp = RecordingTransport(max_packet)
    mcu = MT7601UMcu(tp)
    mcu.mcu_running = True
    phy = MT7601UPhy(tp, mcu, MT7601UEepromParams())
    return MT7601UInit(tp, mcu, phy), tp


SRC = Path(__file__).resolve().parents[3] / "driver_sources" / "mt7601u-source-v7.2" / "mt7601u"
"""Absent in a clone, since driver_sources/ is gitignored; the source-derived
tests below skip rather than fall back to a literal."""


def source_entry_count(header: str, table: str) -> int | None:
    """Count a table's { reg, value } entries straight out of the C.

    initvals.h packs several entries per line, so counting lines undercounts --
    that is how bbp_chip_vals lost 86 of its 150 entries. Count braces instead.
    """
    path = SRC / header
    if not path.is_file():
        return None
    import re
    text = path.read_text()
    match = re.search(rf"{table}\[\] = \{{", text)
    if match is None:
        return None
    end = re.search(r"^\}", text[match.end():], re.M)
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", text[match.end():match.end() + end.start()], flags=re.S)
    return len(re.findall(r"\{\s*\w+(?:\(\s*\d+\s*\))?|\d+\s*,", body))


class TestGeneratedTables:
    """Every table's size is checked against the C, never against a literal.

    bbp_chip_vals is 150, not 64: initvals.h packs several { reg, value } entries
    onto one line, so a parser that matched only the first per line dropped 86 of
    them -- and a test that asserted the wrong literal made that look correct.
    """

    @pytest.mark.parametrize("name", ["rf_central", "rf_channel", "rf_vga"])
    def test_phy_table_sizes_match_the_kernel_header(self, name: str) -> None:
        # These come from RF_REG_PAIR macros, so count the macro calls.
        path = SRC / "initvals_phy.h"
        if not path.is_file():
            pytest.skip("kernel sources not fetched")
        import re
        text = path.read_text()
        match = re.search(rf"{name}\[\] = \{{", text)
        assert match is not None, f"{name} not found in initvals_phy.h"
        end = re.search(r"^\}", text[match.end():], re.M)
        body = re.sub(r"/\*.*?\*/|//[^\n]*", "",
                      text[match.end():match.end() + end.start()], flags=re.S)
        want = len(re.findall(r"RF_REG_PAIR\(", body))
        assert len(getattr(initvals_phy, name)) == want

    @pytest.mark.parametrize("name", [
        "mac_common_vals", "mac_chip_vals", "bbp_common_vals", "bbp_chip_vals",
    ])
    def test_mac_table_sizes_match_the_kernel_header(self, name: str) -> None:
        want = source_entry_count("initvals.h", name)
        if want is None:
            pytest.skip("kernel sources not fetched")
        assert len(getattr(initvals_mac, name)) == want

    @pytest.mark.parametrize("name", [
        "bbp_normal_temp", "bbp_normal_temp_bw20", "bbp_normal_temp_bw40",
        "bbp_high_temp", "bbp_high_temp_bw20", "bbp_high_temp_bw40",
        "bbp_low_temp", "bbp_low_temp_bw20", "bbp_low_temp_bw40",
    ])
    def test_temperature_table_sizes_match_the_kernel_header(self, name: str) -> None:
        want = source_entry_count("initvals_phy.h", name)
        if want is None:
            pytest.skip("kernel sources not fetched")
        assert len(getattr(initvals_phy, name)) == want

    def test_bbp_mode_table_indexes_those_nine_by_mode_and_bandwidth(self) -> None:
        assert [[len(col) for col in row] for row in initvals_phy.bbp_mode_table] == [
            [len(initvals_phy.bbp_normal_temp_bw20),
             len(initvals_phy.bbp_normal_temp_bw40),
             len(initvals_phy.bbp_normal_temp)],
            [len(initvals_phy.bbp_high_temp_bw20),
             len(initvals_phy.bbp_high_temp_bw40),
             len(initvals_phy.bbp_high_temp)],
            [len(initvals_phy.bbp_low_temp_bw20),
             len(initvals_phy.bbp_low_temp_bw40),
             len(initvals_phy.bbp_low_temp)],
        ]

    def test_entries_are_bare_offsets_not_pre_based(self) -> None:
        """mcu.c adds the base itself; a pre-based table would double it and land
        the whole table in the wrong memory map."""
        for name in ("mac_common_vals", "mac_chip_vals",
                     "bbp_common_vals", "bbp_chip_vals"):
            for addr, _v in getattr(initvals_mac, name):
                assert addr < 0x10000

    def test_wire_addresses_land_in_the_right_memmap(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        tp.payloads.clear()
        _init.write_mac_initvals()
        sent = [a for i in range(len(tp.payloads)) for a, _v in tp.pairs_of(i)]
        assert all(a & ~0xFFFF == C.MT_MCU_MEMMAP_WLAN for a in sent)

    def test_bbp_wire_addresses_land_in_the_bbp_memmap(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        tp.payloads.clear()
        _init.init_bbp()
        sent = [a for i in range(len(tp.payloads)) for a, _v in tp.pairs_of(i)]
        assert all(a & ~0xFFFF == C.MT_MCU_MEMMAP_BBP for a in sent)

    def test_phy_entries_carry_their_bank_and_offset(self) -> None:
        for a, _v in initvals_phy.rf_central:
            assert a & ~0xFFFF == C.MT_MCU_MEMMAP_RF

    def test_indexed_macro_entries_are_present(self) -> None:
        """MT_BCN_OFFSET(0..1) are written as indexed macros in the C table; a
        parser that only matches bare names silently drops them."""
        addrs = {a & 0xFFFF for a, _v in initvals_mac.mac_chip_vals}
        assert C.MT_BCN_OFFSET(0) in addrs
        assert C.MT_BCN_OFFSET(1) in addrs
        assert len(initvals_mac.mac_chip_vals) == 17


class TestBeaconOffsets:
    def test_sixteen_slots_512_bytes_apart(self) -> None:
        assert len(BEACON_OFFSETS) == 16
        assert BEACON_OFFSETS[1] - BEACON_OFFSETS[0] == 0x200

    def test_loop_agrees_with_the_table_where_both_write(self) -> None:
        """init.c mt76_init_beacon_offsets and mac_chip_vals compute the same
        value for slots 0 and 1, so the port can be cross-checked two ways."""
        table = {a & 0xFFFF: v for a, v in initvals_mac.mac_chip_vals}  # bare offsets
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_beacon_offsets()
        for i in range(2):
            want = table[C.MT_BCN_OFFSET(i)]
            assert tp.writes_to(C.MT_BCN_OFFSET(i)) == [want]

    def test_slots_two_and_three_come_only_from_the_loop(self) -> None:
        table = {a & 0xFFFF for a, _v in initvals_mac.mac_chip_vals}
        assert C.MT_BCN_OFFSET(2) not in table
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_beacon_offsets()
        assert tp.writes_to(C.MT_BCN_OFFSET(2))
        assert tp.writes_to(C.MT_BCN_OFFSET(3))


class TestUsbDma:
    def test_three_writes_in_order(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_usb_dma()
        writes = tp.writes_to(C.MT_USB_DMA_CFG)
        assert len(writes) == 3
        assert writes[1] & C.MT_USB_DMA_CFG_UDMA_RX_WL_DROP
        assert not writes[2] & C.MT_USB_DMA_CFG_UDMA_RX_WL_DROP

    def test_aggregation_enabled_for_512_byte_endpoints(self) -> None:
        _init, tp = make_init(USB_DMA_AGG_EN_MAX_PACKET)
        tp.ops.clear()
        _init.init_usb_dma()
        assert tp.writes_to(C.MT_USB_DMA_CFG)[0] & C.MT_USB_DMA_CFG_RX_BULK_AGG_EN

    def test_aggregation_disabled_for_small_endpoints(self) -> None:
        _init, tp = make_init(64)
        tp.ops.clear()
        _init.init_usb_dma()
        assert not tp.writes_to(C.MT_USB_DMA_CFG)[0] & C.MT_USB_DMA_CFG_RX_BULK_AGG_EN

    def test_aggregation_follows_the_endpoint_discovered_after_construction(self) -> None:
        """assign_pipes() fills transport.in_max_packet inside connect(), long after
        MT7601UInit is constructed. A snapshot taken at construction stays 0, which
        silently disabled aggregation on a dongle whose bulk-IN is 512 bytes."""
        tp = RecordingTransport(0)
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        phy = MT7601UPhy(tp, mcu, MT7601UEepromParams())
        _init = MT7601UInit(tp, mcu, phy)
        _init.init_usb_dma()
        assert not tp.writes_to(C.MT_USB_DMA_CFG)[0] & C.MT_USB_DMA_CFG_RX_BULK_AGG_EN
        tp.in_max_packet = USB_DMA_AGG_EN_MAX_PACKET
        tp.ops.clear()
        _init.init_usb_dma()
        assert tp.writes_to(C.MT_USB_DMA_CFG)[0] & C.MT_USB_DMA_CFG_RX_BULK_AGG_EN

    def test_both_bulk_engines_stay_enabled_through_the_pulse(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_usb_dma()
        for val in tp.writes_to(C.MT_USB_DMA_CFG):
            assert val & C.MT_USB_DMA_CFG_TX_BULK_EN
            assert val & C.MT_USB_DMA_CFG_RX_BULK_EN


class TestCsrBbpReset:
    def test_reset_brackets_a_usb_dma_shutdown(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.reset_csr_bbp()
        kinds = [(op[0], op[1]) for op in tp.ops]
        assert kinds == [("wr", C.MT_MAC_SYS_CTRL), ("wr", C.MT_USB_DMA_CFG),
                         ("wr", C.MT_MAC_SYS_CTRL)]
        vals = tp.writes_to(C.MT_MAC_SYS_CTRL)
        assert vals[0] == C.MT_MAC_SYS_CTRL_RESET_CSR | C.MT_MAC_SYS_CTRL_RESET_BBP
        assert vals[1] == 0


class TestWriteMacInitvals:
    def test_two_wlan_messages_then_beacons_then_aux_clock(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.write_mac_initvals()
        sent = [p for i in range(len(tp.payloads)) for p in tp.pairs_of(i)]
        assert len(sent) == len(initvals_mac.mac_common_vals) + len(initvals_mac.mac_chip_vals)
        assert tp.writes_to(C.MT_AUX_CLK_CFG) == [0]
        assert len(tp.writes_to(C.MT_BCN_OFFSET(3))) == 1

    def test_aux_clock_write_comes_after_the_beacon_offsets(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.write_mac_initvals()
        order = [op[1] for op in tp.ops if op[0] == "wr" and op[1] in
                 (C.MT_BCN_OFFSET(0), C.MT_AUX_CLK_CFG)]
        assert order == [C.MT_BCN_OFFSET(0), C.MT_AUX_CLK_CFG]


class TestInitBbp:
    def test_waits_for_the_bbp_before_writing_the_tables(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_bbp()
        sent = [p for i in range(len(tp.payloads)) for p in tp.pairs_of(i)]
        assert len(sent) == len(initvals_mac.bbp_common_vals) + len(initvals_mac.bbp_chip_vals)


class TestResetCounters:
    def test_reads_the_six_station_counters(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.reset_counters()
        assert [op[1] for op in tp.ops] == [
            C.MT_RX_STA_CNT0, C.MT_RX_STA_CNT1, C.MT_RX_STA_CNT2,
            C.MT_TX_STA_CNT0, C.MT_TX_STA_CNT1, C.MT_TX_STA_CNT2]


class TestMacStart:
    def test_promiscuous_filter_is_installed_between_the_two_enables(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.mac_start()
        vals = tp.writes_to(C.MT_MAC_SYS_CTRL)
        assert vals == [C.MT_MAC_SYS_CTRL_ENABLE_TX,
                        C.MT_MAC_SYS_CTRL_ENABLE_TX | C.MT_MAC_SYS_CTRL_ENABLE_RX]
        assert tp.writes_to(C.MT_RX_FILTR_CFG) == [RX_FILTER_MONITOR]

    def test_filter_drops_the_error_classes_a_monitor_never_asks_for(self) -> None:
        """main.c:117-118 keep CRC_ERR and PHY_ERR set unless mac80211 asks for FCSFAIL
        or PLCPFAIL, which a monitor interface does not."""
        for bit in (C.MT_RX_FILTR_CFG_CRC_ERR, C.MT_RX_FILTR_CFG_PHY_ERR,
                    C.MT_RX_FILTR_CFG_VER_ERR, C.MT_RX_FILTR_CFG_DUP):
            assert RX_FILTER_MONITOR & bit

    def test_filter_admits_other_bss_control_frames_and_pspoll(self) -> None:
        """A monitor asks for FIF_OTHER_BSS, FIF_CONTROL and FIF_PSPOLL, so main.c:116-125
        clears PROMISC, the control group and PSPOLL. RTS is outside that group."""
        for bit in (C.MT_RX_FILTR_CFG_PROMISC, C.MT_RX_FILTR_CFG_ACK,
                    C.MT_RX_FILTR_CFG_CTS, C.MT_RX_FILTR_CFG_CFEND,
                    C.MT_RX_FILTR_CFG_CFACK, C.MT_RX_FILTR_CFG_BA,
                    C.MT_RX_FILTR_CFG_CTRL_RSV, C.MT_RX_FILTR_CFG_PSPOLL):
            assert not RX_FILTER_MONITOR & bit
        assert RX_FILTER_MONITOR & C.MT_RX_FILTR_CFG_RTS

    def test_dma_busy_forever_raises(self) -> None:
        init_, tp = make_init()
        tp.rr = lambda offset: (C.MT_WPDMA_GLO_CFG_TX_DMA_BUSY
                                if offset == C.MT_WPDMA_GLO_CFG else 0)
        with pytest.raises(BringUpError, match="DMA still busy"):
            init_.mac_start()


class TestChipOnoff:
    def test_enable_sets_the_clock_and_the_enable_bits(self) -> None:
        init_, tp = make_init()
        readback = C.MT_CMB_CTRL_XTAL_RDY | C.MT_CMB_CTRL_PLL_LD
        tp.rr = lambda offset: readback
        init_.chip_onoff(True)
        # init.c:82 writes the register back untouched before init.c:32 gates the clock.
        writes = tp.writes_to(C.MT_WLAN_FUN_CTRL)
        assert len(writes) == 2
        assert writes[0] == readback
        assert writes[1] & C.MT_WLAN_FUN_CTRL_WLAN_EN
        assert writes[1] & C.MT_WLAN_FUN_CTRL_WLAN_CLK_EN
        assert init_.wlan_running

    def test_disable_clears_enable_but_keeps_the_clock(self) -> None:
        """init.c:20 -- dropping WLAN_CLK stops the chip answering the probe path."""
        init_, tp = make_init()
        tp.rr = lambda offset: 0xFFFFFFFF            # WLAN_EN and WLAN_CLK_EN both set
        init_.chip_onoff(False)
        val = tp.writes_to(C.MT_WLAN_FUN_CTRL)[-1]
        assert not val & C.MT_WLAN_FUN_CTRL_WLAN_EN
        assert val & C.MT_WLAN_FUN_CTRL_WLAN_CLK_EN
        assert not init_.wlan_running

    def test_crystal_never_locking_logs_and_continues(self, caplog) -> None:
        """init.c:55 only logs; refusing here would strand a card upstream brings up."""
        init_, tp = make_init()
        tp.rr = lambda offset: 0
        with caplog.at_level(logging.ERROR):
            init_.chip_onoff(True)
        assert "PLL and XTAL" in caplog.text
        assert init_.wlan_running


class TestAggregateConstants:
    def test_values_come_from_mt7601uh(self) -> None:
        assert MT_USB_AGGR_TIMEOUT == 0x80
        assert MT_USB_AGGR_SIZE_LIMIT == 28

    def test_timeout_and_limit_land_in_their_fields(self) -> None:
        _init, tp = make_init()
        tp.ops.clear()
        _init.init_usb_dma()
        val = tp.writes_to(C.MT_USB_DMA_CFG)[0]
        assert C._field_get(C.MT_USB_DMA_CFG_RX_BULK_AGG_TOUT, val) == MT_USB_AGGR_TIMEOUT
        assert C._field_get(C.MT_USB_DMA_CFG_RX_BULK_AGG_LMT, val) == MT_USB_AGGR_SIZE_LIMIT


class TestPollReadCounts:
    """core.c:33 is `timeout /= 10` and core.c:43 tests after the read: timeout/10 + 1."""

    @staticmethod
    def _never_settles() -> tuple[MT7601UInit, list[int]]:
        init_, tp = make_init()
        reads: list[int] = []

        def rr(offset: int) -> int:
            reads.append(offset)
            return 0xFFFFFFFF

        tp.rr = rr
        return init_, reads

    def test_microsecond_poll_reads_six_times_for_fifty(self) -> None:
        init_, reads = self._never_settles()
        assert init_.poll(C.MT_MAC_STATUS, 0xF, 0, 50) is False
        assert len(reads) == 6              # init.c:250 mt76_poll(..., 50)

    def test_millisecond_poll_reads_eleven_times_for_a_hundred(self) -> None:
        init_, reads = self._never_settles()
        assert init_.poll_msec(C.MT_MAC_STATUS, 0xF, 0, 100) is False
        assert len(reads) == 11             # init.c:364 mt76_poll_msec(..., 100)

    def test_mac_idle_wait_raises_after_the_c_s_eleven_reads(self) -> None:
        init_, reads = self._never_settles()
        with pytest.raises(BringUpError, match="MAC_STATUS"):
            init_.poll_mac_idle()
        assert len(reads) == 11

    def test_a_settled_register_is_read_once(self) -> None:
        init_, tp = make_init()
        tp.ops.clear()
        assert init_.poll(C.MT_MAC_STATUS, 0xF, 0, 200_000) is True
        assert len([op for op in tp.ops if op[0] == "read"]) == 1


class TestMacStopHw:
    """init.c:257 mt7601u_mac_stop_hw."""

    def test_it_clears_the_beacon_timers_first(self) -> None:
        init_, tp = make_init()
        tp.ops.clear()
        init_.mac_stop_hw()
        first_write = next(op for op in tp.ops if op[0] == "wr")
        assert first_write[1] == C.MT_BEACON_TIME_CFG
        timers = (C.MT_BEACON_TIME_CFG_TIMER_EN | C.MT_BEACON_TIME_CFG_SYNC_MODE
                  | C.MT_BEACON_TIME_CFG_TBTT_EN | C.MT_BEACON_TIME_CFG_BEACON_TX)
        assert first_write[2] & timers == 0

    def test_tx_and_rx_are_disabled_together(self) -> None:
        init_, tp = make_init()
        tp.ops.clear()
        init_.mac_stop_hw()
        vals = tp.writes_to(C.MT_MAC_SYS_CTRL)
        assert vals == [0]                  # init.c:283 clears both enables in one write

    def test_the_rx_drain_needs_seven_clean_passes(self) -> None:
        """init.c:292's `ok++ > 5` tests the pre-increment value, so the seventh pass exits."""
        init_, tp = make_init()
        reads: list[int] = []
        tp.rr = lambda offset: (reads.append(offset), 0)[1]
        init_._drain_rx_page_counts()
        assert reads.count(C.MT_RXQ_STA) == 7

    def test_the_tx_drain_stops_on_the_first_clean_pass(self) -> None:
        init_, tp = make_init()
        reads: list[int] = []
        tp.rr = lambda offset: (reads.append(offset), 0)[1]
        init_._drain_tx_page_counts()
        assert reads == [C.MT_PCNT_0438, C.MT_PCNT_0A30, C.MT_PCNT_0A34]

    def test_a_queue_that_never_drains_gives_up_after_two_hundred_passes(
            self, monkeypatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        init_, tp = make_init()
        reads: list[int] = []
        tp.rr = lambda offset: (reads.append(offset), 0xFFFFFFFF)[1]
        init_._drain_tx_page_counts()
        # init.c:273's first read short-circuits the other two when it is non-zero.
        assert reads == [C.MT_PCNT_0438] * QUEUE_DRAIN_PASSES

    def test_a_stuck_poll_warns_rather_than_raising(self, caplog, monkeypatch) -> None:
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        init_, tp = make_init()
        tp.rr = lambda offset: 0xFFFFFFFF
        with caplog.at_level(logging.WARNING):
            init_.mac_stop_hw()             # init.c only dev_warn()s on every poll here
        assert "TX DMA did not stop" in caplog.text
        assert "RX DMA did not stop" in caplog.text
