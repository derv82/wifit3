"""EvilTwin: a WPA2 twin that punts clients off a WPA3-transition AP and captures their 4-way.

``FakeAP`` (fake_ap.py) owns the twin's beacon, responder, and per-client state; the orchestrating
``EvilTwinCampaign`` (campaign.py) elects the two interfaces, runs the punt, and detects completion.
"""
from .fake_ap import FakeAP, FakeApStats, ClientProgress, ClientPhase

__all__ = ["FakeAP", "FakeApStats", "ClientProgress", "ClientPhase"]
