"""M3: init_cal — carrier-leak, ar9271 pa_cal, noise-floor load/start, IQ-cal setup.

All register reads are mocked to 0, so the AGC/NF wait loops see their busy bits clear and
terminate after one read — exactly the feedback the cold-boot capture recorded.
"""
import struct

from wifit3.chips.ar9271_v2 import calib, chan as chanmod, hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Replies to every WMI command; REG_READ returns one zero u32 per requested address."""

    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        payload = data[12:]
        self.cmds.append((cmd_id, payload))
        if cmd_id == 0x14:                                  # WMI_REG_READ — N addrs -> N vals
            nvals = max(1, len(payload) // 4)
            body = b"\x00\x00\x00\x00" * nvals
        else:
            body = struct.pack(">I", 0)                     # rsp_status
        self._resp = struct.pack(">BBH", 1, 0, len(body) + 4) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + body
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.rxchainmask = 1
    calib.init_cal(h, chanmod.channel_2ghz(1))
    return dev


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b), 12)]
    return out


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b), 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _write_list(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x15:
            out += [struct.unpack_from(">II", b, k) for k in range(0, len(b), 8)]
    return out


def test_cl_cal_carrier_leak_sequence():
    dev = _run()
    rmw = _rmws(dev)
    # enable CL cal, power up ADC, filter-cal + PD-cal, run AGC cal, then tear down.
    assert (R.AR_PHY_CL_CAL_CTL, R.AR_PHY_CL_CAL_ENABLE, 0) in rmw
    assert (R.AR_PHY_ADC_CTL, 0, R.AR_PHY_ADC_CTL_OFF_PWDADC) in rmw
    assert (R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_FLTR_CAL, 0) in rmw
    assert (R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_PD_CAL_ENABLE, 0) in rmw
    assert (R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_CAL, 0) in rmw
    assert (R.AR_PHY_CL_CAL_CTL, 0, R.AR_PHY_CL_CAL_ENABLE) in rmw


def test_pa_cal_top2_write_and_buffer_block():
    dev = _run()
    rmw = _rmws(dev)
    # the localmode/synthon magic write, then the pre-loop buffer's first and last RMWs.
    assert (R.AR9285_AN_TOP2, 0xCA0358A0) in _write_list(dev)
    assert (R.AR9285_AN_RF2G6, 0, 1 << 0) in rmw
    assert (R.AR9285_AN_RF2G1, 0, R.AR9285_AN_RF2G1_PDPADRV1) in rmw
    # the v6.18-only tail RMWs must NOT be emitted (this firmware omits them).
    assert (R.AR9285_AN_RF2G1, 0, 0x01000000) not in rmw     # PDPADRV2
    assert (R.AR9285_AN_RF2G1, 0, 0x00800000) not in rmw     # PDPAOUT


def test_pa_cal_block_has_exactly_ten_rmws():
    dev = _run()
    # the pre-loop buffered block flushes as one 10-entry RMW command (matches the wire).
    rmw_batches = [b for c, b in dev.cmds if c == 0x20]
    ten = [b for b in rmw_batches if len(b) // 12 == 10]
    assert len(ten) == 1


def test_loadnf_writes_nominal_then_minus50():
    dev = _run()
    rmw = _rmws(dev)
    # chain-0 NF reg gets the band nominal (-118) then the -50 maxCCApower restore.
    assert (R.AR_PHY_CCA, ((-118 << 1) & 0x1FF), 0x1FF) in rmw
    assert (R.AR_PHY_CCA, ((-50 << 1) & 0x1FF), 0x1FF) in rmw
    # only chain 0 — the 1T1R mask never reaches chains 1/2 or the HT40-only ext chain.
    assert all(reg != 0xA864 for reg, _, _ in rmw)


def test_start_nfcal_and_iq_setup():
    dev = _run()
    w, rmw = _writes(dev), _rmws(dev)
    assert (R.AR_PHY_AGC_CONTROL, R.AR_PHY_AGC_CONTROL_ENABLE_NF, 0) in rmw
    assert (R.AR_PHY_AGC_CONTROL, 0, R.AR_PHY_AGC_CONTROL_NO_UPDATE_NF) in rmw
    # IQ-cal setup: calCountMax field, CALMODE=IQ, DO_CAL.
    assert (R.AR_PHY_TIMING_CTRL4_0,
            R.SM(R.PER_MAX_LOG_COUNT, R.AR_PHY_TIMING_CTRL4_IQCAL_LOG_COUNT_MAX),
            R.AR_PHY_TIMING_CTRL4_IQCAL_LOG_COUNT_MAX) in rmw
    assert w[R.AR_PHY_CALMODE] == R.AR_PHY_CALMODE_IQ
    assert (R.AR_PHY_TIMING_CTRL4_0, R.AR_PHY_TIMING_CTRL4_DO_CAL, 0) in rmw
