"""Campaign — the radio-owning attack base class.

A campaign owns the half-duplex radio for its lifetime: at most one runs at a
time (the class-level ``active`` slot is the mutex). You ``run()`` a campaign; the
base owns the lifecycle from there — scheduling the work as a task, guaranteeing
``teardown()`` on every exit (natural completion, user stop, or crash), and the
cooperative ``stopped`` flag — so each subclass writes only its *behaviour*:
``_loop()`` + ``teardown()``, plus the ``visible()`` / ``ineligible_reason()``
classmethods the Focus button row derives from.

Engine-pure: no Textual. The Activity-Log sink is injected as a :class:`TreeLog`
handle so the bytes a campaign writes are identical to today's
``screen._log(treelog.branch(m))`` wiring.

Status/headline/card rendering deliberately stays in ``ui.focus_model`` for now
(read off the active campaign); per-campaign ``status_*`` properties are a
separate, test-guarded pass.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import treelog

logger = logging.getLogger(__name__)


class TreeLog:
    """A campaign's live handle to the Activity Log. Wraps the pure ``treelog``
    formatters with an injected ``sink`` (the screen's ``_log`` in the app, a
    ``list.append`` in tests) so a campaign writes ``self.treelog.branch("…")``
    and the bytes that reach the log are identical to today's
    ``_log(treelog.branch(m))`` call sites."""

    def __init__(self, sink):
        self._sink = sink

    def line(self, msg: str) -> None: self._sink(msg)                 # raw, undecorated
    def header(self, msg: str) -> None: self._sink(treelog.header(msg))
    def branch(self, msg: str) -> None: self._sink(treelog.branch(msg))
    def branch_ok(self, msg: str) -> None: self._sink(treelog.branch_ok(msg))
    def branch_fail(self, msg: str) -> None: self._sink(treelog.branch_fail(msg))
    def branch_dim(self, msg: str) -> None: self._sink(treelog.branch_dim(msg))
    def leaf(self, msg: str) -> None: self._sink(treelog.leaf(msg))
    def leaf_ok(self, msg: str) -> None: self._sink(treelog.leaf_ok(msg))
    def leaf_fail(self, msg: str) -> None: self._sink(treelog.leaf_fail(msg))
    def leaf_warn(self, msg: str) -> None: self._sink(treelog.leaf_warn(msg))


class Campaign:
    """Base for every radio-owning attack. One subclass per campaign; the
    campaign's own complexity lives behind ``_loop()``."""

    # The radio mutex: at most one campaign is ``active`` across ALL subclasses
    # (one half-duplex card). ``start()`` refuses while it's set; the ``_run``
    # wrapper clears it on exit. Assign via ``Campaign.active`` (the class attr),
    # never ``self.active`` (which would shadow it with an instance attr).
    active: Optional["Campaign"] = None

    button_id: Optional[str] = None   # the Focus button this campaign owns; None = no button (PBC)
    key: str = ""                     # mutex/registry identity: "wep"/"wps"/"wpa3down"/"pmkid"/"pbc"
    stoppable: bool = True            # False = fire-once; button stays disabled, never flips to "Stop X"
    # Button text/variant the registry-driven derive_buttons paints: idle_* when
    # this campaign is not running, run_* while it owns the radio (the "Stop X").
    idle_label: str = ""
    run_label: str = ""
    idle_variant: str = "primary"
    run_variant: str = "error"

    def __init__(self, ap, iface, treelog: Optional[TreeLog] = None):
        self.ap = ap
        self.iface = iface
        self.treelog = treelog
        self.stopped = False
        self._task: Optional[asyncio.Task] = None

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
        # teardown() lives in the task's own frame so it fires on EVERY exit —
        # natural return, stop-induced break, or a crash — which an external
        # awaiter of the task could never guarantee for the natural-completion case.
        # The framework is the crash backstop: an unexpected error in _loop() is
        # logged (to the engine logger, never the TUI) rather than propagated, so
        # one campaign blowing up can't break the screen or wedge the radio. The
        # campaign owns its *expected* outcomes itself (it logs them). The mutex
        # slot is cleared in the outermost finally so even a teardown() crash
        # releases the radio.
        try:
            try:
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
            # Only release the slot if WE still hold it — a synchronous
            # request_stop() may have freed it already and another campaign may
            # have since claimed it; an unguarded clear would clobber that one.
            if Campaign.active is self:
                Campaign.active = None

    async def stop(self) -> None:
        """Cooperative stop: raise the flag, then await the loop draining +
        teardown. Safe to call after natural completion (the task is already done)."""
        self.stopped = True
        if self._task is not None:
            await self._task

    def request_stop(self) -> None:
        """Synchronous fire-and-forget stop for sync callers (the screen): flag the
        stop and free the radio slot NOW, leaving the running task to drain its loop
        and run teardown on its own. Freeing the slot synchronously lets the next
        run() claim the radio immediately — matching the pre-base
        ``create_task(stop())`` semantics without the one-tick refusal race."""
        self.stopped = True
        if Campaign.active is self:
            Campaign.active = None

    @property
    def done(self) -> bool:
        """True once ``_loop()`` + ``teardown()`` have finished — the screen polls
        this to extract results + drop its handle."""
        return self._task is not None and self._task.done()

    # ---- behaviour (subclass fills these) -----------------------------------
    async def _loop(self) -> None:
        """The campaign's work — the ``while not self.stopped`` loop (or a bounded
        one-shot). MUST poll ``self.stopped`` before every blocking call, and keep
        waits bounded, so a stop lands promptly."""
        raise NotImplementedError

    async def teardown(self) -> None:
        """Exit-driven cleanup — the campaign's own (deauth-or-not, clear fake
        MAC, save state, stop sub-transports). Default no-op."""

    @classmethod
    def visible(cls, ap) -> bool:
        """Is this campaign relevant to this target's encryption family at all?
        False → no button, no log line (e.g. a WEP attack on a WPA target)."""
        return False

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        """Given it's visible: ``None`` = eligible (enabled); a string = disabled,
        and that string is the once-at-load 'why' (e.g. ``"PMF:Required"``).
        Mirrors the attack's real pre-flight preconditions."""
        return None


# The campaign registry: order is button-row + headline/dispatch priority.
# Populated as each campaign migrates onto the base (Phase C).
CAMPAIGNS: list[type[Campaign]] = []
