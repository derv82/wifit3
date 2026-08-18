import os
import re
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select

from wifit3.chips.driver import FakeMacSupport
from wifit3.wlan.array import fake_mac_rank
from wifit3.ui.screens.focus_v2.art import display_name
from wifit3.campaigns.eviltwin import (
    EvilTwinInput, PuntMode, default_punt_modes, csa_target_channel,
)

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

# (label, punt_period_sec, punt_once)
_CYCLES = [("Never", None, False), ("Once", 30.0, True), ("5 seconds", 5.0, False),
           ("10 seconds", 10.0, False), ("20 seconds", 20.0, False), ("30 seconds", 30.0, False),
           ("45 seconds", 45.0, False), ("60 seconds", 60.0, False)]
_DEFAULT_CYCLE = 5


def _plus_one(bssid: str) -> str:
    return bssid[:-1] + format((int(bssid[-1], 16) + 1) % 16, "x")


def _random_bssid() -> str:
    return "02:" + ":".join(f"{b:02x}" for b in os.urandom(5))


class EvilTwinInputModal(ModalScreen[Optional[EvilTwinInput]]):
    """Pick the twin/punter interfaces, BSSID, channel and punt method before EvilTwin starts."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    DEFAULT_CSS = """
    EvilTwinInputModal { align: center middle; }
    EvilTwinInputModal #dialog {
        width: 64; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    EvilTwinInputModal #title { content-align: center middle; margin-bottom: 1; text-style: bold; }
    EvilTwinInputModal .field { height: auto; margin-bottom: 1; }
    EvilTwinInputModal .field-label { color: $text-muted; }
    EvilTwinInputModal #bssid-row Button { margin-left: 1; min-width: 8; }
    EvilTwinInputModal #warn { color: $text-warning; content-align: center middle; height: auto; }
    EvilTwinInputModal #button-row { dock: bottom; height: auto; align: center middle; }
    EvilTwinInputModal #button-row Button { margin: 0 1; }
    """

    def __init__(self, target, members: List) -> None:
        super().__init__()
        self.target = target
        self._single = len(members) == 1     # one card: host + punt share the target's channel
        self._hosts = sorted((m for m in members if _can_host(m)), key=fake_mac_rank)
        self._punters = list(members)
        self._by_name = {m.name: m for m in members}

    def compose(self) -> ComposeResult:
        twin = self._hosts[0] if self._hosts else None
        punter = next((m for m in self._punters if m is not twin), twin)
        d_modes = default_punt_modes(self.target)
        pmf = self.target.pmf_required
        with Vertical(id="dialog"):
            yield Label("Evil Twin", id="title")

            yield Label("EvilTwin interface", classes="field-label")
            yield Select([(display_name(m), m.name) for m in self._hosts],
                         value=twin.name if twin else Select.BLANK,
                         allow_blank=False, id="twin-iface", classes="field")

            yield Label(f"EvilTwin channel  (target on CH {self.target.channel})",
                        classes="field-label")
            yield Select(self._channel_options(twin), value=self._default_channel(twin),
                         allow_blank=False, id="twin-channel", classes="field",
                         disabled=self._single)

            yield Label("EvilTwin BSSID", classes="field-label")
            with Horizontal(id="bssid-row", classes="field"):
                yield Input(value=self._default_bssid(), id="twin-bssid")
                yield Button("Same", id="bssid-same")
                yield Button("+1", id="bssid-plus1")
                yield Button("Random", id="bssid-random")

            yield Label("Punter interface", classes="field-label")
            yield Select([(display_name(m), m.name) for m in self._punters],
                         value=punter.name if punter else Select.BLANK,
                         allow_blank=False, id="punt-iface", classes="field")

            with Horizontal(classes="field"):
                yield Checkbox("De-auth", value=PuntMode.DEAUTH in d_modes,
                               id="punt-deauth", disabled=pmf)
                yield Checkbox("Chan. Switch Ann.", value=PuntMode.CSA in d_modes, id="punt-csa")
                yield Checkbox("BSS-Trans.", value=PuntMode.BTM in d_modes,
                               id="punt-btm", disabled=pmf)

            yield Label("Punt cycle", classes="field-label")
            yield Select([(label, i) for i, (label, *_) in enumerate(_CYCLES)],
                         value=_DEFAULT_CYCLE, allow_blank=False, id="punt-cycle", classes="field")

            yield Label("", id="warn")
            with Horizontal(id="button-row"):
                yield Button("Start EvilTwin", variant="primary", id="btn-start")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self._sync_bssid_field()
        self._sync_warning()
        if self.target.pmf_required:                # robust-frame punts can't be forged under PMF
            self.query_one("#punt-deauth", Checkbox).tooltip = "Can't send deauths: PMF active"
            self.query_one("#punt-btm", Checkbox).tooltip = "Can't send BTMs: PMF active"

    # ----- interface / channel wiring ---------------------------------------

    def _channel_options(self, twin) -> List:
        if self._single:                        # locked to the target: one card can't host off-channel
            return [(f"CH {self.target.channel}", self.target.channel)]
        chans = twin.supported_channels if twin else [self.target.channel]
        return [(f"CH {c}" + ("  (same as target)" if c == self.target.channel else ""), c)
                for c in chans]

    def _default_channel(self, twin) -> int:
        if self._single:
            return self.target.channel
        chans = twin.supported_channels if twin else [self.target.channel]
        for c in (csa_target_channel(self.target.channel), self.target.channel):
            if c in chans:
                return c
        return chans[0]

    def _default_bssid(self) -> str:
        return _plus_one(self.target.bssid) if self._single else self.target.bssid

    def _selected(self, widget_id: str):
        return self._by_name.get(self.query_one(f"#{widget_id}", Select).value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "twin-iface":
            twin = self._selected("twin-iface")
            channel = self.query_one("#twin-channel", Select)
            channel.set_options(self._channel_options(twin))
            channel.value = self._default_channel(twin)
            self._sync_bssid_field()
        self._sync_warning()

    def _sync_bssid_field(self) -> None:
        twin = self._selected("twin-iface")
        spoofable = twin is not None and twin.driver.FAKE_MAC is FakeMacSupport.SPOOFABLE
        bssid = self.query_one("#twin-bssid", Input)
        bssid.disabled = not spoofable
        for bid in ("bssid-same", "bssid-plus1", "bssid-random"):
            self.query_one(f"#{bid}", Button).disabled = not spoofable
        if not spoofable and twin is not None:
            bssid.value = twin.mac_address or self.target.bssid

    def _sync_warning(self) -> None:
        same_iface = self._selected("twin-iface") is self._selected("punt-iface")
        self.query_one("#warn", Label).update(
            "EvilTwin works best with 2 different interfaces" if same_iface else "")

    # ----- buttons -----------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        bssid = self.query_one("#twin-bssid", Input)
        if bid == "bssid-same":
            bssid.value = self.target.bssid
        elif bid == "bssid-plus1":
            bssid.value = _plus_one(self.target.bssid)
        elif bid == "bssid-random":
            bssid.value = _random_bssid()
        elif bid == "btn-start":
            self._start()
        elif bid == "btn-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _start(self) -> None:
        twin = self._selected("twin-iface")
        punter = self._selected("punt-iface")
        channel = self.query_one("#twin-channel", Select).value
        twin_bssid = self.query_one("#twin-bssid", Input).value.strip().lower()
        if not _MAC_RE.match(twin_bssid):
            self._error("Invalid BSSID")
            return
        if twin_bssid == self.target.bssid and channel == self.target.channel:
            self._error("Same BSSID on the same channel collides; change one")
            return
        _, period, once = _CYCLES[self.query_one("#punt-cycle", Select).value]
        self.dismiss(EvilTwinInput(
            twin_iface=twin, punt_iface=punter, twin_channel=channel, twin_bssid=twin_bssid,
            punt_modes=self._punt_modes(), csa_channel=None, punt_period_sec=period, punt_once=once))

    def _punt_modes(self) -> tuple[PuntMode, ...]:
        checked = {"punt-deauth": PuntMode.DEAUTH, "punt-csa": PuntMode.CSA, "punt-btm": PuntMode.BTM}
        return tuple(mode for bid, mode in checked.items()
                     if self.query_one(f"#{bid}", Checkbox).value)

    def _error(self, text: str) -> None:
        self.query_one("#warn", Label).update(f"[red]{text}[/red]")


def _can_host(iface) -> bool:
    return getattr(getattr(iface, "driver", None), "FAKE_MAC", None) in (
        FakeMacSupport.SPOOFABLE, FakeMacSupport.FIXED_MAC)
