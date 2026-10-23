"""Protected Management Frames (802.11w) posture from the RSN capability bits.

MFPC (bit 7) = PMF capable, MFPR (bit 6) = PMF required; pure spec, no device.
"""
from enum import StrEnum


class PmfPosture(StrEnum):
    DISABLED = "disabled"     # MFPC=0, MFPR=0: management frames unprotected
    OPTIONAL = "optional"     # MFPC=1, MFPR=0: protected only for PMF-capable clients
    REQUIRED = "required"     # MFPC=1, MFPR=1: every client must use PMF
    INVALID = "invalid"       # MFPC=0, MFPR=1: malformed (required without capable)
