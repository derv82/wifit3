"""WEP attack suite (fake-auth, ARP replay, ChopChop, PTW crack).

The orchestrator ``WepCampaign`` lives in ``campaign.py``; it is re-exported here
so callers use ``from wifit3.campaigns.wep import WepCampaign``.
"""
from wifit3.campaigns.wep.campaign import WepCampaign

__all__ = ["WepCampaign"]
