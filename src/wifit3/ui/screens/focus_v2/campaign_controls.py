"""Starter, stopper, and maintainer of the currently-active campaign."""
from __future__ import annotations

from typing import Optional

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.wep import WepCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign, EvilTwinInput


class CampaignControls:
    def __init__(self) -> None:
        self._campaign: Optional[Campaign] = None   # current campaign

    @property
    def current(self) -> Optional[Campaign]:
        """Current active campaign, or None."""
        return self._campaign

    def start(self, campaign: type[Campaign], array, ap, *,
              log=None, evil_input: Optional[EvilTwinInput] = None) -> Optional[Campaign]:
        """Construct and run one campaign, None if another campaign is active.

        Constructors differ per campaign (intentionally not unified): wep takes
        ``log_callback``, EvilTwin takes its ``evil_input`` dataclass, the rest take ``log``.
        """
        if Campaign.active is not None or self._campaign is not None:
            return None
        if campaign is WepCampaign:
            inst = campaign(array, ap, log_callback=log)
        elif campaign is EvilTwinCampaign:
            inst = campaign(array, ap, evil_input)
        else:
            inst = campaign(array, ap, log=log)
        inst.run()
        self._campaign = inst
        return inst

    def request_stop(self) -> None:
        """Ask the running campaign to stop; keep it so ``reap()`` still logs its result."""
        if self._campaign is not None:
            self._campaign.request_stop()

    def stop(self) -> None:
        """Stop and forget immediately (leaving the screen); no result is reaped."""
        if self._campaign is not None:
            self._campaign.request_stop()
            self._campaign = None

    def reap(self) -> Optional[Campaign]:
        """The just-finished campaign (cleared), or None while it is still running."""
        camp = self._campaign
        if camp is not None and camp.done:
            self._campaign = None
            return camp
        return None
