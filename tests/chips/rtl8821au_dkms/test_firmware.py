from wifit3.chips.rtl8821au_dkms import constants as C
from wifit3.chips.rtl8821au_dkms import firmware


def test_download_firmware_rewrites_the_image_after_a_failed_page_write(mocker):
    t = mocker.Mock()
    t.read8.return_value = 0
    t.read32.return_value = C.FWDL_ChkSum_rpt | C.WINTINI_RDY
    t.writeN.side_effect = [False, True]
    fw_blob = bytes(C.FW_HEADER_SIZE + 196)
    assert firmware.download_firmware(t, fw_blob, delay=lambda s: None) is True
    assert t.writeN.call_count == 2

def test_download_firmware_logs_each_step(mocker, caplog):
    t = mocker.Mock()
    t.read8.return_value = 0
    t.read32.return_value = C.FWDL_ChkSum_rpt | C.WINTINI_RDY
    t.writeN.side_effect = [False, True]
    with caplog.at_level("INFO"):
        firmware.download_firmware(t, bytes(C.FW_HEADER_SIZE + 196), delay=lambda s: None)
    assert caplog.messages[0] == "_BlockWrite_8812 phase 1 i:0"
    assert caplog.messages[1].startswith("polling_fwdl_chksum: Checksum report OK! (1, ")
    assert caplog.messages[2].startswith("_FWFreeToGo8812: Polling FW ready OK! (1, ")
    assert caplog.messages[3].startswith("FWDL success. write_fw:2, ")
