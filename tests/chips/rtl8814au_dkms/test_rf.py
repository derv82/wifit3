"""Hardware-free regression for the M2c RF radio tables + RCK1 copy.

The full byte-for-byte check vs the cold-boot capture is
`scripts/chips/rtl8814au_dkms/verify_pcap.py`; this pins the RF write/read encoding and
the per-path taken-row counts.
"""
from wifit3.chips.rtl8814au_dkms import constants as C
from wifit3.chips.rtl8814au_dkms import phy_cond, rf


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def write32(self, a, v):
        self.ops.append(("W", a, v))

    def read32(self, a):
        self.ops.append(("R", a, self.reads.get(a, 0)))
        return self.reads.get(a, 0)


def test_rf_write_encoding():
    rec = Rec()
    rf._rf_write(rec, "a", 0x018, 0x00013124)
    # (0x018 << 20) | 0x13124 -> 0xc90 (path A).
    assert rec.ops == [("W", 0x0C90, 0x01813124)]


def test_rf_read_is_memory_mapped():
    # RF reg 0x1c on path A reads 0x2800 + 0x1c*4 = 0x2870, masked to 20 bits.
    rec = Rec(reads={0x2870: 0x00053952})
    assert rf._rf_read(rec, "a", C.RF_RCK1) == 0x53952
    assert rec.ops == [("R", 0x2870, 0x00053952)]


def test_rf_delay_addr_emits_no_write(monkeypatch):
    monkeypatch.setattr(rf.time, "sleep", lambda *_: None)
    rec = Rec()
    rf._rf_emit(rec, "a")(0xFE, 0x0)        # 0xfe -> 50 ms delay, not a write
    assert rec.ops == []


def test_copy_rck1_reads_a_writes_bcd():
    rec = Rec(reads={0x2870: 0x00053952})
    rf._copy_rck1(rec)
    assert rec.ops == [
        ("R", 0x2870, 0x00053952),
        ("W", C.RF_LSSI_WRITE["b"], 0x01C53952),
        ("W", C.RF_LSSI_WRITE["c"], 0x01C53952),
        ("W", C.RF_LSSI_WRITE["d"], 0x01C53952),
    ]


def test_radio_tables_taken_write_counts():
    """rfe=1 selects the 1176 RF writes (radio_a..d) seen on the cold-boot wire."""
    d1 = phy_cond.build_driver1(1)
    total = 0
    for _, table in rf._RADIO:
        n = 0

        def emit(a, v):
            nonlocal n
            # 0xfe/0xffe are delays, not register writes — match the wire count.
            if a not in C.RF_DELAY_ADDRS:
                n += 1

        phy_cond.walk_table(table, d1, emit)
        total += n
    assert total == 1176
