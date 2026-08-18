"""Campaign: the radio-owning attack base class.

A campaign owns the half-duplex radio for its lifetime: at most one runs at a
time (the class-level ``active`` slot is the mutex). You ``run()`` a campaign; the
base owns the lifecycle from there: scheduling the work as a task, guaranteeing
``teardown()`` on every exit (natural completion, user stop, or crash), and the
cooperative ``stopped`` flag. So each subclass writes only its *behaviour*:
``_loop()`` + ``teardown()``, plus the ``visible()`` / ``ineligible_reason()``
classmethods the Focus button row derives from.

Engine-pure: no Textual, kept free of any UI dependency so the attack logic
is testable in isolation.

Status/headline/card rendering deliberately stays in ``ui.focus_model`` for now
(read off the active campaign); per-campaign ``status_*`` properties are a
separate, test-guarded pass.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Campaign:
    """Base for every radio-owning attack. One subclass per campaign; the
    campaign's own complexity lives behind ``_loop()``."""

    # The radio mutex: at most one campaign is ``active`` across ALL subclasses
    active: Optional["Campaign"] = None

    button_id: Optional[str] = None   # the Focus button this campaign owns; None = no button (PBC)
    key: str = ""                     # mutex/registry identity: "wep"/"wps"/"eviltwin"/"pmkid"/"pbc"
    # Focus footer command: (keycap, short label), or None for no hotkey
    hotkey: Optional[tuple[str, str]] = None
    stoppable: bool = True            # False = fire-once; button stays disabled, never flips to "Stop X"
    # Button text/variant the registry-driven derive_buttons paints: idle_* when
    # this campaign is not running, run_* while it owns the radio (the "Stop X").
    idle_label: str = ""
    run_label: str = ""
    idle_variant: str = "primary"
    run_variant: str = "error"

    def __init__(self, ap, array):
        self.ap = ap
        self.array = array
        self._iface = None                 # elected radio, resolved lazily by the `iface` property
        self.stopped = False
        self._task: Optional[asyncio.Task] = None

    @property
    def iface(self):
        """The radio this campaign drives, elected from the array on first use: a card that can tune
        to the target's channel. None when no live card can reach the band, in which case ``_loop``
        is skipped."""
        if self._iface is None and self.array is not None:
            self._iface = self.array.select_iface(self.ap.channel)
        return self._iface

    # ---- lifecycle (framework-owned; subclasses do NOT override) ------------
    def run(self) -> bool:
        """Run this campaign: claim the radio and schedule its ``_loop()`` in the
        background. Returns False (a no-op) if a campaign already owns the radio."""
        if Campaign.active is not None:
            return False
        Campaign.active = self
        self.stopped = False
        self._task = asyncio.create_task(self._drive())
        return True

    async def _drive(self) -> None:
        try:
            try:
                if self.array is not None and self.iface is None:
                    logger.warning("campaign %r: no card can reach channel %s; aborting",
                                   self.key, getattr(self.ap, "channel", "?"))
                else:
                    await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("campaign %r crashed in _loop()", self.key)
            finally:
                try:
                    await self.teardown()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("campaign %r crashed in teardown()", self.key)
        finally:
            # Only release the slot if WE still hold it
            if Campaign.active is self:
                Campaign.active = None

    async def stop(self) -> None:
        """Cooperative stop."""
        self.stopped = True
        if self._task is not None:
            await self._task

    def request_stop(self) -> None:
        """Synchronous fire-and-forget stop for sync callers (the screen)."""
        self.stopped = True
        if Campaign.active is self:
            Campaign.active = None

    @property
    def done(self) -> bool:
        """True once ``_loop()`` + ``teardown()`` have finished."""
        return self._task is not None and self._task.done()

    # ---- behaviour (subclass fills these) -----------------------------------
    async def _loop(self) -> None:
        """The campaign's work."""
        raise NotImplementedError

    async def teardown(self) -> None:
        """Exit-driven cleanup: the campaign's own (deauth-or-not, clear fake
        MAC, save state, stop sub-transports). Default no-op."""

    @classmethod
    def visible(cls, ap) -> bool:
        """Is this campaign relevant to this target's encryption family at all?
        False → no button, no log line (e.g. a WEP attack on a WPA target)."""
        return False

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        return None
