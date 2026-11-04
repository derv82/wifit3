import struct
from unittest.mock import MagicMock

from wifit3.chips.rtl8812au_dkms import tx
from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver

AP = bytes.fromhex("020000000001")
STA = bytes.fromhex("020000000002")
BROADCAST = b"\xff" * 6


def frame(fc0: int, addr1: bytes, addr3: bytes) -> bytes:
    return bytes([fc0, 0]) + b"\x00\x00" + addr1 + STA + addr3 + b"\x00\x00" + b"\xaa" * 30


def desc_dwords(payload: bytes) -> tuple[int, ...]:
    return struct.unpack_from("<10I", payload, 0)


async def injected_desc(channel: int, mpdu: bytes) -> tuple[int, ...]:
    transport = MagicMock()
    driver = Rtl8812auDkmsDriver(transport)
    driver._channel = channel
    assert await driver._inject_frame(mpdu)
    return desc_dwords(transport.bulk_out.call_args.args[0])


async def test_unicast_data_frame_takes_vht1ss_mcs9_and_the_2t2r_rate_id():
    # rtw_xmit.c:4924-4931, update_monitor_frame_attrib (rtw_mlme_ext.c:7671): RF_2T2R -> RATEID_IDX_VHT_2SS.
    dw = await injected_desc(1, frame(0x08, AP, AP))
    assert (dw[1] >> 16) & 0x1F == tx.RATEID_IDX_VHT_2SS
    assert dw[4] & 0x7F == tx.DESC_RATEVHTSS1MCS9
    assert (dw[2] >> 24) & 0x3F == 0                     # no stainfo for a unicast ra


async def test_broadcast_data_frame_takes_24m():
    dw = await injected_desc(1, frame(0x08, AP, BROADCAST))
    assert dw[4] & 0x7F == tx.DESC_RATE24M


async def test_mgmt_frame_takes_the_mlmeext_tx_rate():
    dw = await injected_desc(1, frame(0xC0, BROADCAST, AP))
    assert (dw[1] >> 16) & 0x1F == tx.RATEID_IDX_B
    assert dw[4] & 0x7F == tx.DESC_RATE1M
    assert dw[0] & (1 << 24)                             # BMC
    assert (dw[2] >> 24) & 0x3F == tx.TXBF_G_ID_BCMC
    dw = await injected_desc(36, frame(0xC0, AP, AP))
    assert (dw[1] >> 16) & 0x1F == tx.RATEID_IDX_G
    assert dw[4] & 0x7F == tx.DESC_RATE6M


def test_mgnt_frametag_fields():
    payload = tx.update_txdesc(frame(0xC0, AP, AP), rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
    dw = desc_dwords(payload)
    assert dw[1] & 0x7F == tx.RTW_DEFAULT_MGMT_MACID
    assert (dw[1] >> 8) & 0x1F == tx.QSLT_MGNT
    assert dw[3] & (1 << 8)                              # USE_RATE
    assert dw[4] & (1 << 17)                             # RETRY_LIMIT_ENABLE
    assert (dw[4] >> 18) & 0x3F == tx.MGNT_DATA_RETRY_LIMIT
    assert dw[8] & (1 << 15)                             # HWSEQ_EN
    assert (dw[1] >> 24) & 0x1F == 0                     # pulled: pkt_offset 1 -> 0
    assert len(payload) == 40 + len(frame(0xC0, AP, AP))


def test_bulk_size_boundary_keeps_the_packet_offset():
    mpdu = frame(0xC0, AP, AP) + b"\x00" * (512 - 40 - len(frame(0xC0, AP, AP)))
    payload = tx.update_txdesc(mpdu, rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
    assert (desc_dwords(payload)[1] >> 24) & 0x1F == 1
    assert len(payload) == 40 + tx.PACKET_OFFSET_SZ + len(mpdu)
