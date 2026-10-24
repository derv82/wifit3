import pytest

from wifit3.dot11.pmf import PmfPosture
from wifit3.models import AccessPoint


@pytest.mark.parametrize(
    "capable, required, posture",
    [
        (False, False, PmfPosture.DISABLED),
        (True, False, PmfPosture.OPTIONAL),
        (True, True, PmfPosture.REQUIRED),
        (False, True, PmfPosture.INVALID),
    ],
)
def test_access_point_pmf_posture(capable: bool, required: bool, posture: PmfPosture) -> None:
    ap = AccessPoint(bssid="02:00:00:00:00:01", pmf_capable=capable, pmf_required=required)
    assert ap.pmf_posture is posture
