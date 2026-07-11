"""set_board_values EEPROM-variant branches (generalization beyond the reference card).

The reference card is modal-header v4, txGainType 0, bb_desired_scale 0 — so these branches are
inert on it and the pcap gate proves the reference bytes are unchanged. These tests exercise the
runtime-gated paths that a *different* AR9271 EEPROM would take, against the vendor C:
  * the ob/db modal-version remap (v0 / v1 vs v>=2) [SRC] eeprom_4k.c:826-859
  * the txGainType==0 smart-antenna bb_desired_scale TX-pwrctrl block [SRC] eeprom_4k.c:1007
"""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy_board, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI
from wifit3.chips.ar9271_v2.eeprom_4k import Map4k

from .test_txpower import EEPROM

_MODAL = 52                                # base_eep_header_4k(32) + custData(20)
_OFF_TXGAINTYPE = 31                       # baseEepHeader.txGainType
_OFF_MODAL_VER = _MODAL + 37
_OFF_BBSCALE = _MODAL + 46                 # modalHeader.bb_scale_smrt_antenna
# ob/db packed-nibble byte offsets (low nibble = [0]/[even], high nibble = [1]/[odd])
_OFF_OB_01 = _MODAL + 25                   # ob_0 | ob_1<<4
_OFF_OB_23 = _MODAL + 38                   # ob_2 | ob_3<<4
_OFF_DB1_01 = _MODAL + 26                  # db1_0 | db1_1<<4
_OFF_DB2_01 = _MODAL + 36                  # db2_0 | db2_1<<4


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", 0)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run(eeprom: bytes):
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.macVersion = R.AR_SREV_VERSION_9271
    h.eeprom = bytearray(eeprom)
    phy_board.set_board_values(h, chanmod.channel_2ghz(1))
    return dev


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b) - 4, 12)]
    return out


def _variant(edits: dict[int, int]) -> bytes:
    b = bytearray(EEPROM)
    for off, val in edits.items():
        b[off] = val
    return bytes(b)


# ---- ob/db modal-version remap ------------------------------------------------

# distinct nibbles so each version selects observably different ob[2] / db2[0]. The RF2G3/RF2G4
# OB/DB fields are 3 bits wide, so keep every nibble <= 7: ob_0=5 ob_1=6 ob_2=7, db1_0=4, db2_0=6.
_DISTINCT = dict(zip(
    (_OFF_OB_01, _OFF_OB_23, _OFF_DB1_01, _OFF_DB2_01),
    (0x65, 0x07, 0x04, 0x06)))


def _ob_rmw(rmw):
    """map (ob_cck, ob_psk, ob_qam) programmed into RF2G3."""
    def field(mask):
        for reg, s, clr in rmw:
            if reg == R.AR9285_AN_RF2G3 and clr == mask:
                return R.MS(s, mask)
        return None
    return (field(R.AR9271_AN_RF2G3_OB_cck), field(R.AR9271_AN_RF2G3_OB_psk),
            field(R.AR9271_AN_RF2G3_OB_qam))


def _db2_rmw(rmw):
    for reg, s, clr in rmw:
        if reg == R.AR9285_AN_RF2G4 and clr == R.AR9271_AN_RF2G4_DB_2:
            return R.MS(s, R.AR9271_AN_RF2G4_DB_2)
    return None


def test_version_ge2_distinct_nibbles():
    eep = _variant({_OFF_MODAL_VER: 2, **_DISTINCT})
    assert Map4k(eep).modal_version == 2
    rmw = _rmws(_run(eep))
    assert _ob_rmw(rmw) == (5, 6, 7)          # ob_0/ob_1/ob_2 taken as-is
    assert _db2_rmw(rmw) == 6                  # db2_0


def test_version1_replicates_ob1():
    # v1: ob[1..4] <- ob_1, so ob_qam (ob[2]) becomes ob_1=6, not ob_2=7. db2[0] stays db2_0.
    eep = _variant({_OFF_MODAL_VER: 1, **_DISTINCT})
    rmw = _rmws(_run(eep))
    assert _ob_rmw(rmw) == (5, 6, 6)
    assert _db2_rmw(rmw) == 6


def test_version0_replicates_ob0_and_db2_from_db1():
    # v0: ob[all] <- ob_0=5; db2[all] <- db1_0=4 (the kernel quirk, not db2_0).
    eep = _variant({_OFF_MODAL_VER: 0, **_DISTINCT})
    rmw = _rmws(_run(eep))
    assert _ob_rmw(rmw) == (5, 5, 5)
    assert _db2_rmw(rmw) == 4


# ---- bb_desired_scale TX-pwrctrl block ---------------------------------------

def test_bb_scale_zero_emits_no_pwrctrl():
    # Reference layout: txGainType 0, bb_scale 0 -> the block is skipped entirely.
    regs = {reg for reg, _s, _c in _rmws(_run(EEPROM))}
    assert R.AR_PHY_TX_PWRCTRL8 not in regs
    assert R.AR_PHY_CH0_TX_PWRCTRL13 not in regs


def test_bb_scale_nonzero_txgain0_programs_pwrctrl():
    eep = _variant({_OFF_TXGAINTYPE: 0, _OFF_BBSCALE: 3})
    rmw = _rmws(_run(eep))
    m6 = (1 << 0) | (1 << 5) | (1 << 10) | (1 << 15) | (1 << 20) | (1 << 25)
    m3 = (1 << 0) | (1 << 5) | (1 << 15)
    m2 = (1 << 0) | (1 << 5)
    assert (R.AR_PHY_TX_PWRCTRL8, m6 * 3, m6 * 0x1f) in rmw
    assert (R.AR_PHY_TX_PWRCTRL10, m6 * 3, m6 * 0x1f) in rmw
    assert (R.AR_PHY_CH0_TX_PWRCTRL12, m6 * 3, m6 * 0x1f) in rmw
    assert (R.AR_PHY_TX_PWRCTRL9, m3 * 3, m3 * 0x1f) in rmw
    assert (R.AR_PHY_CH0_TX_PWRCTRL11, m2 * 3, m2 * 0x1f) in rmw
    assert (R.AR_PHY_CH0_TX_PWRCTRL13, m2 * 3, m2 * 0x1f) in rmw


def test_bb_scale_nonzero_txgain_high_skips_pwrctrl():
    # txGainType != 0 -> the block does not run even with a non-zero scale.
    eep = _variant({_OFF_TXGAINTYPE: 1, _OFF_BBSCALE: 3})
    regs = {reg for reg, _s, _c in _rmws(_run(eep))}
    assert R.AR_PHY_TX_PWRCTRL8 not in regs
