"""The ported ath9k_init_firmware_version major/minor screen (bringup._require_supported_fw)."""
import pytest

from wifit3.chips.ar9271_v2 import bringup, constants as C
from wifit3.errors import BringUpError


def test_accepts_shipped_and_minimum():
    bringup._require_supported_fw(1, 4)                                             # htc_9271-1.4.0.fw
    bringup._require_supported_fw(C.FW_VERSION_MAJOR_REQ, C.FW_VERSION_MINOR_REQ)   # exact minimum


def test_rejects_older_minor():
    with pytest.raises(BringUpError):
        bringup._require_supported_fw(1, C.FW_VERSION_MINOR_REQ - 1)


def test_rejects_foreign_major():
    with pytest.raises(BringUpError):
        bringup._require_supported_fw(2, 9)
    with pytest.raises(BringUpError):
        bringup._require_supported_fw(0, 0)
