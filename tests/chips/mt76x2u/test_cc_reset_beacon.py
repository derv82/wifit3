"""`mac_cc_reset` + `init_beacon_config` register-level coverage.

Kernel reference: mt76x02_mac.c:1213-1229 (cc_reset),
mt76x02_beacon.c:205-213 (init_beacon_config).
"""
from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import mac


class FakeTransport:
    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.rmws: list[tuple[int, int, int]] = []
        self.reads: list[int] = []
        self.regs: dict[int, int] = {}

    def read32(self, addr: int) -> int:
        self.reads.append(addr)
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))

    def rmw32(self, addr: int, mask: int, value: int) -> None:
        cur = self.regs.get(addr, 0)
        self.regs[addr] = (cur & ~mask) | (value & mask)
        self.rmws.append((addr, mask, value))


# ---------------------------------------------------------------------------
# mac_cc_reset
# ---------------------------------------------------------------------------

def test_cc_reset_writes_ch_time_cfg_with_kernel_bits():
    """Kernel mt76x02_mac.c:1217: TIMER_EN|TX|RX|NAV|EIFS_AS_BUSY|
    CH_CCA_RC_EN | (CH_TIMER_CLR=1)."""
    t = FakeTransport()
    mac.mac_cc_reset(t)
    expected = (
        C.MT_CH_TIME_CFG_TIMER_EN
        | C.MT_CH_TIME_CFG_TX_AS_BUSY
        | C.MT_CH_TIME_CFG_RX_AS_BUSY
        | C.MT_CH_TIME_CFG_NAV_AS_BUSY
        | C.MT_CH_TIME_CFG_EIFS_AS_BUSY
        | C.MT_CH_CCA_RC_EN
        | (1 << C.MT_CH_TIME_CFG_CH_TIMER_CLR_SHIFT)
    )
    assert (C.MT_CH_TIME_CFG, expected) in t.writes


def test_cc_reset_reads_busy_and_idle_counters_to_clear():
    """Kernel reads MT_CH_BUSY + MT_CH_IDLE to clear them post-write."""
    t = FakeTransport()
    mac.mac_cc_reset(t)
    assert C.MT_CH_BUSY in t.reads
    assert C.MT_CH_IDLE in t.reads


def test_cc_reset_ch_time_cfg_value_matches_kernel_exact_pattern():
    """Spot-check the literal bit pattern: bits 0-4 + bit 6 + bits 8-9 (=1)."""
    t = FakeTransport()
    mac.mac_cc_reset(t)
    val = next(v for a, v in t.writes if a == C.MT_CH_TIME_CFG)
    # bits 0-4 set
    assert val & 0x1F == 0x1F
    # bit 6 set
    assert val & (1 << 6)
    # bits 8-9: CH_TIMER_CLR field = 1
    assert (val >> 8) & 0x3 == 1


# ---------------------------------------------------------------------------
# init_beacon_config — BEACON_TIME_CFG bits
# ---------------------------------------------------------------------------

def test_init_beacon_config_clears_timer_tbtt_beacon_tx_bits():
    """First RMW clears TIMER_EN | TBTT_EN | BEACON_TX (mask = those bits,
    value = 0)."""
    t = FakeTransport()
    mac.init_beacon_config(t)
    clear_mask = (
        C.MT_BEACON_TIME_CFG_TIMER_EN
        | C.MT_BEACON_TIME_CFG_TBTT_EN
        | C.MT_BEACON_TIME_CFG_BEACON_TX
    )
    matched = [
        (a, m, v) for a, m, v in t.rmws
        if a == C.MT_BEACON_TIME_CFG and m == clear_mask and v == 0
    ]
    assert matched, f"BEACON_TIME_CFG clear RMW not found in {t.rmws}"


def test_init_beacon_config_sets_sync_mode_bits():
    """Second RMW sets SYNC_MODE (mask = SYNC_MODE_MASK, value = same)."""
    t = FakeTransport()
    mac.init_beacon_config(t)
    mask = C.MT_BEACON_TIME_CFG_SYNC_MODE_MASK
    matched = [
        (a, m, v) for a, m, v in t.rmws
        if a == C.MT_BEACON_TIME_CFG and m == mask and v == mask
    ]
    assert matched, f"SYNC_MODE set RMW not found in {t.rmws}"


def test_init_beacon_config_writes_bypass_mask_ffff():
    t = FakeTransport()
    mac.init_beacon_config(t)
    assert (C.MT_BCN_BYPASS_MASK, 0xFFFF) in t.writes


def test_init_beacon_config_writes_4_beacon_offset_regs():
    """Per Task 6 — but already wired into the same function."""
    t = FakeTransport()
    mac.init_beacon_config(t)
    expected_addrs = [C.MT_BCN_OFFSET_BASE + i * 4 for i in range(4)]
    written_offset_addrs = sorted({
        a for a, _ in t.writes
        if C.MT_BCN_OFFSET_BASE <= a < C.MT_BCN_OFFSET_BASE + 16
    })
    assert written_offset_addrs == sorted(expected_addrs)


def test_init_beacon_config_writes_only_4_offset_regs_not_5():
    """N_BCN_SLOTS=5 but the offsets pack into 4 u32 regs (1 byte per slot
    in regs[0..3], 4 slots in regs[0] + 1 slot in regs[1])."""
    t = FakeTransport()
    mac.init_beacon_config(t)
    offset_writes = [
        a for a, _ in t.writes
        if C.MT_BCN_OFFSET_BASE <= a < C.MT_BCN_OFFSET_BASE + 16
    ]
    assert len(offset_writes) == 4


def test_init_beacon_config_offset_values_match_kernel_packing():
    """Slots: 0, 1600, 3200, 4800, 6400; values: 0, 25, 50, 75, 100.

    regs[0] = 0 | (25<<8) | (50<<16) | (75<<24) = 0x4B321900
    regs[1] = 100 = 0x64
    regs[2] = 0
    regs[3] = 0
    """
    t = FakeTransport()
    mac.init_beacon_config(t)
    offset_writes = {
        a: v for a, v in t.writes
        if C.MT_BCN_OFFSET_BASE <= a < C.MT_BCN_OFFSET_BASE + 16
    }
    assert offset_writes[C.MT_BCN_OFFSET_BASE + 0] == 0x4B321900
    assert offset_writes[C.MT_BCN_OFFSET_BASE + 4] == 0x64
    assert offset_writes[C.MT_BCN_OFFSET_BASE + 8] == 0
    assert offset_writes[C.MT_BCN_OFFSET_BASE + 12] == 0
