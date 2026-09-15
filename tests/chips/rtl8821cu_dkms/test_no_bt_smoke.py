"""Hardware-free smoke test: the whole no-BT (bt_coexist=FALSE) cold bring-up runs to completion
with NO unhandled exception (issue #53 — the `t.btc` AttributeError on the wifi-only band switch).

The pcap-gated reference card is a combo (bt_coexist=TRUE), so the byte-for-byte gate never walks
the wifi-only branches; the reporter's card is a no-BT die and crashed the first channel tune. This
drives the *real* ``driver.connect()`` (which with no running loop runs ``bringup.cold_bringup``:
EFUSE parse -> power-on -> FW download -> MAC/BB/RF/PHYDM init -> the wifi-only front-end config ->
the ch1 band-switch antenna route -> monitor entry) over a permissive fake transport whose EFUSE
decodes to a VALID, non-combo, rfe-0x22 / 1-Ant-main card. Only USB device I/O is faked; every
bring-up branch runs product code. A trailing 5 GHz tune exercises the no-BT band switch again.

The no-BT branches this exercises (each a place the combo path would touch ``t.btc``):
  * ``bringup.power_on`` / ``power_off``  — skip the BT-coex power/scoreboard setting
  * ``bringup.hal_init``                  — ``btcwifionly.hw_config`` (GNT owner + coex tables)
  * ``chan.set_channel`` band switch      — ``btcwifionly.switch_antenna`` (the #53 crash site)
  * ``bringup.set_monitor_mode``          — no media-connect BT-coex notify

Keepable regression insurance for exactly the reported bug; easily deleted if unwanted.
"""
from wifit3.chips.rtl8821cu_dkms import efuse
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver


def _pack_block(blk: int, words: dict) -> bytes:
    """Emit one physical-EFUSE block (``eeprom_parser`` encoding): a 1-byte header for blk < 16,
    else the 2-byte extended header, then the low/high byte of each enabled 16-bit word."""
    word_en = 0x0F
    for w in words:
        word_en &= ~(1 << w)                      # a word is ENABLED when its word_en bit is clear
    out = bytearray()
    if blk < 16:
        out.append((blk << 4) | word_en)
    else:
        out.append(((blk & 0x07) << 5) | 0x0F)    # ext header marker (hdr & 0x1F == 0x0F)
        out.append(((blk >> 3) << 4) | word_en)
    for w in range(4):
        if w in words:
            lo, hi = words[w]
            out += bytes((lo, hi))
    return bytes(out)


def _efuse_phys_map() -> bytes:
    """A 512-B physical EFUSE that decodes to a valid, no-BT reference burn:
    EEPROM-ID 0x8129 (map valid), board-option interface_sel 0 (NON-combo -> bt_coexist FALSE),
    RF_BT_SETTING 0x41 (1-Ant @ main), RFE option 0x22 (BTG / cut-4 reference, a defined arm)."""
    m = bytearray(b"\xff" * efuse.EFUSE_SIZE_8821C)
    body = (
        _pack_block(0, {0: (0x29, 0x81)})                     # log[0:2] = 0x8129 -> map valid
        + _pack_block(24, {0: (0xFF, 0x00), 1: (0xFF, 0x41)})  # 0xC1 board opt / 0xC3 RF_BT_SETTING
        + _pack_block(25, {1: (0x22, 0xFF)})                   # 0xCA RFE option 0x22
    )
    m[:len(body)] = body
    return bytes(m)


class _FakeTransport:
    """Permissive stand-in for ``Rtl8821cuTransport``: serves the EFUSE dump + the poll/branch reads
    the bring-up waits on, records writes/bulk-OUT, and defaults every other read to 0. Reads are
    FIXED (non-stateful) on purpose — the auto-LLT / pwr-seq polls wait for the HW to CLEAR a
    just-written bit, so a read-your-writes store would deadlock them. It never grows a ``btc``."""

    _EFUSE_CTRL = 0x0030
    # Poll-ready / branch values the cold path needs (everything else reads 0):
    _READS = {
        0x0080: 0xC078,   # REG_MCUFW_CTRL: FW ready (also the checksum-OK gate + FSPI-clear branch)
        0x01A0: 0x19,     # C2HEVT_MSG_NORMAL: MAC-hidden report ready marker (C2H_MAC_HIDDEN_RPT)
        0x00F1: 0x40,     # SYS_CFG1+1 >> 4 = 4 -> chip_ver / cut 4 (the reference cut, not A-cut)
        0x00F5: 0x01,     # SYS_STATUS1+1 bit0 set -> chip reads as OFF, so the cold pwr-seq runs
        0x0006: 0x02,     # pwr-seq CARDEMU->ACT polling: bit1 set
        0x0205: 0x80,     # FIFOPAGE_CTRL_2+1: rsvd-page bcn-valid bit7
        0x1703: 0x20,     # LTECOEX_CTRL+3: read/write-ready bit5
        0x041A: 0xFF,     # TXPKT_EMPTY: FIFO reads empty
        0x041B: 0x06,     # TXPKT_EMPTY+1: empty-check bits
    }

    def __init__(self, phys_map: bytes):
        self.phys_map = phys_map
        self.writes: list[tuple] = []
        self.bulks: list[bytes] = []
        self._efuse_addr = 0
        # the session state the real transport carries (bring-up / chan read + mutate these)
        self.last_hme_box = 0
        self.current_band = None
        self.current_channel = None
        self.thermal_reset_pending = False
        self.cck_new_agc = False
        self.cck_agc_report_type = 1
        self.rega24 = self.rega28 = self.regaac = 0

    def _read(self, addr: int, width: int) -> int:
        if addr == self._EFUSE_CTRL:
            return 0x80000000 | self.phys_map[self._efuse_addr & 0x1FF]   # EF_FLAG | data byte
        return self._READS.get(addr, 0) & ((1 << (8 * width)) - 1)

    def read8(self, addr: int) -> int:
        return self._read(addr, 1)

    def read16(self, addr: int) -> int:
        return self._read(addr, 2)

    def read32(self, addr: int) -> int:
        return self._read(addr, 4)

    def write8(self, addr: int, val: int) -> None:
        self.writes.append(("W8", addr, val & 0xFF))

    def write16(self, addr: int, val: int) -> None:
        self.writes.append(("W16", addr, val & 0xFFFF))

    def write32(self, addr: int, val: int) -> None:
        if addr == self._EFUSE_CTRL:
            self._efuse_addr = (val >> 8) & 0x3FF          # latch the byte address for the next read
        self.writes.append(("W32", addr, val & 0xFFFFFFFF))

    def bulk_out(self, data: bytes) -> None:
        self.bulks.append(bytes(data))

    def close(self) -> None:
        pass


def _drive(coro) -> object:
    """Run a driver coroutine that never suspends on a real await (no running loop -> the sync
    bring-up path) straight to its return value, mirroring the pcap gate's synchronous driver."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine suspended on a real await; the no-loop path should be sync")


def _w32(t: _FakeTransport) -> list:
    return [op for op in t.writes if op[0] == "W32"]


def test_no_bt_cold_bringup_completes_without_touching_btc(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)   # pwr-seq LDO settle: no real waiting

    driver = Rtl8821cuDkmsDriver(object())      # dummy USB dev; the transport does I/O, and we fake it
    t = _FakeTransport(_efuse_phys_map())
    driver.transport = t

    # (1) The whole no-BT cold bring-up runs to completion with NO unhandled exception. This is the
    # reporter's exact path: connect -> cold_bringup -> init_hw_mlme_ext -> chan.set_channel (the
    # 2.4 GHz band switch that crashed on t.btc) -> set_monitor_mode.
    assert _drive(driver.connect()) is True
    assert driver.info is not None and driver.info.bt_coexist is False       # the no-BT path was taken

    # (2) The combo path never ran: t.btc is the BtcState only btc.hal_init creates.
    assert getattr(t, "btc", None) is None

    # (3) The wifi-only front-end config ran (bringup.hal_init else-arm -> btcwifionly.hw_config).
    for expected in (
        ("W32", 0x0070, 0x04000000),        # GNT owner -> WL
        ("W32", 0x1704, 0x00007700),        # gnt_wl=1 / gnt_bt=0 wdata
        ("W32", 0x1700, 0xC00F0038),        # LTE-coex indirect write 0x38
        ("W32", 0x06C0, 0xAAAAAAAA),
        ("W32", 0x06C4, 0xAAAAAAAA),
    ):
        assert expected in _w32(t), expected

    # (4) The 2.4 GHz band switch routed through btcwifionly.switch_antenna (its DPDT writes) —
    # exactly the op that raised AttributeError('btc') before the wifi-only port existed.
    for expected in (
        ("W32", 0x004C, 0x01000000),        # DPDT-SW select (0x4c[24:23])
        ("W32", 0x0CB4, 0x00000077),        # DPDT control pins (0xcb4 low byte)
        ("W32", 0x0CB4, 0x10000000),        # DPDT polarity (0xcb4[29:28] -> main, non-inverted)
    ):
        assert expected in _w32(t), expected

    # (5) A 5 GHz tune re-runs the no-BT band switch (btcwifionly.switch_antenna, is_5g=True) — the
    # other band's crash site — and must likewise complete without touching t.btc.
    w32_before = _w32(t)
    assert _drive(driver.set_channel(36)) is True
    assert getattr(t, "btc", None) is None
    assert ("W32", 0x004C, 0x01000000) in _w32(t)[len(w32_before):]     # the 5 GHz switch ran anew
