"""Verify campaigns: one per stored credential type, checked live against the target."""
from wifit3.models import CaptureType

from .pin import VerifyPinCampaign
from .psk import VerifyPskCampaign
from .wep import VerifyWepCampaign

VERIFY_CAMPAIGNS = {CaptureType.WPA_PSK: VerifyPskCampaign, 
                    CaptureType.WPS_PIN: VerifyPinCampaign,
                    CaptureType.WEP: VerifyWepCampaign}
