"""M2a: the HTC handshake emits the exact connect/config/complete frames and learns the
endpoint map from the target's responses."""
import pytest

from wifit3.chips.ar9271_v2 import constants as C, htc
from wifit3.chips.ar9271_v2.transport import AR9271Transport

# Canned REG_IN (EP 0x83) responses captured from a real cold boot, in order: HTC_READY,
# 9 connect-service responses (service -> endpoint), then the config-pipe response.
_READY = bytes.fromhex("00000008000000000001002101401600")
_CONN_RSPS = [
    bytes.fromhex("0000000a0000000000030100000106040000"),   # WMI_CONTROL -> ep1
    bytes.fromhex("0000000a0000000000030101000206400000"),   # BEACON      -> ep2
    bytes.fromhex("0000000a0000000000030102000306400000"),   # CAB         -> ep3
    bytes.fromhex("0000000a0000000000030103000406400000"),   # UAPSD       -> ep4
    bytes.fromhex("0000000a0000000000030104000506400000"),   # MGMT        -> ep5
    bytes.fromhex("0000000a0000000000030107000606400000"),   # DATA_BE     -> ep6
    bytes.fromhex("0000000a0000000000030108000706400000"),   # DATA_BK     -> ep7
    bytes.fromhex("0000000a0000000000030106000806400000"),   # DATA_VI     -> ep8
    bytes.fromhex("0000000a0000000000030105000906400000"),   # DATA_VO     -> ep9
]
_CONFIG_RSP = bytes.fromhex("000000040000000000060100")


class FakeDev:
    def __init__(self, reg_in_queue):
        self.writes = []                       # (ep, data)
        self._reg_in = list(reg_in_queue)

    def write(self, ep, data, timeout=None):
        self.writes.append((ep, bytes(data)))
        return len(data)

    def read(self, ep, length, timeout=None):
        assert ep == C.EP_REG_IN
        return bytearray(self._reg_in.pop(0))


def test_handshake_frames_and_endpoint_map():
    dev = FakeDev([_READY, *_CONN_RSPS, _CONFIG_RSP])
    st = htc.handshake(AR9271Transport(dev))

    # 9 service connects + config-pipe + setup-complete, all on the REG_OUT pipe.
    assert len(dev.writes) == 11
    assert all(ep == C.EP_REG_OUT for ep, _ in dev.writes)

    # First connect == WMI_CONTROL_SVC: hdr(ep0,len10) + msg_id 2, svc 0x0100, dl=3, ul=4.
    assert dev.writes[0][1] == bytes.fromhex("0000000a0000000000020100000003040000")

    # config-pipe credits (msg 5, pipe USB_WLAN_TX_PIPE=1, credits 33=0x21) + setup-complete (msg 4).
    assert dev.writes[9][1] == bytes.fromhex("000000040000000000050121")
    assert dev.writes[10][1] == bytes.fromhex("00000002000000000004")

    # Endpoint map learned from the responses.
    assert st.endpoints[C.WMI_CONTROL_SVC] == 1
    assert st.endpoints[C.WMI_MGMT_SVC] == 5
    assert st.credit_size == 0x0140


def test_process_ready_rejects_stale_reg_in_prefix():
    # The real crash: stale bytes ahead of a valid HTC_READY.
    crash = bytes.fromhex("00c60000") + _READY
    with pytest.raises(htc.HTCReadyError) as ei:
        htc.process_ready(AR9271Transport(FakeDev([crash])), htc.HTCState())
    assert ei.value.raw == crash


def test_process_ready_rejects_short_frame():
    with pytest.raises(htc.HTCReadyError):
        htc.process_ready(AR9271Transport(FakeDev([bytes.fromhex("0000000800")])), htc.HTCState())
