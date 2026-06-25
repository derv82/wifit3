"""M1: the cold-boot firmware download emits the exact ath9k_hif_usb_download_fw sequence."""
from wifit3.chips.ar9271_v2 import constants as C, firmware
from wifit3.chips.ar9271_v2.transport import AR9271Transport


class FakeDev:
    """Records every control transfer the transport issues."""

    def __init__(self):
        self.ctrl = []

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        payload = b"" if isinstance(data_or_wLength, int) else bytes(data_or_wLength)
        self.ctrl.append((bmRequestType, bRequest, wValue, wIndex, payload))
        return len(payload)


def test_download_chunks_and_complete():
    dev = FakeDev()
    fw = firmware.load_firmware_blob()
    firmware.download(AR9271Transport(dev), fw)

    # 13 RAM chunks (12x 4096 + tail) + one COMP write.
    n_chunks = (len(fw) + C.FW_CHUNK - 1) // C.FW_CHUNK
    assert len(dev.ctrl) == n_chunks + 1

    # Every op is a vendor host->device write with wIndex 0.
    assert all(bm == C.BMREQ_VENDOR_OUT and widx == 0 for bm, _, _, widx, _ in dev.ctrl)

    # Chunk writes: bRequest 0x30, wValue = (load_addr + offset) >> 8, ascending by 0x10.
    addr = C.AR9271_FIRMWARE
    sent = 0
    for breq, wval in [(o[1], o[2]) for o in dev.ctrl[:-1]]:
        assert breq == C.FIRMWARE_DOWNLOAD
        assert wval == addr >> 8
        chunk_len = min(C.FW_CHUNK, len(fw) - sent)
        addr += chunk_len
        sent += chunk_len
    assert sent == len(fw)

    # The payload bytes streamed equal the blob, in order.
    assert b"".join(o[4] for o in dev.ctrl[:-1]) == fw

    # COMP: bRequest 0x31, wValue = text-entry >> 8, no payload.
    comp = dev.ctrl[-1]
    assert comp[1] == C.FIRMWARE_DOWNLOAD_COMP
    assert comp[2] == C.AR9271_FIRMWARE_TEXT >> 8
    assert comp[4] == b""
