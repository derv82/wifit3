"""``FocusViewV2``: the spatial "router-admin" Focus redesign (landscape v1).

Layout (top to bottom):
- **Top bar** (fixed height): an "action area" on the left (the back button +
  the encryption-conditional attack buttons, all the clickables in one place)
  then the status line, expanding to fill and stay centered.
- **Mid band**: card | packet-dashboard | router. Card and router are fixed-width
  (the art is 20 cells) and vertically centered; the dashboard fills the
  middle. The band's height is capped so the sparklines reach full 2-row height
  and the endpoint columns fit, then extra terminal height goes to the bottom.
- **Bottom band**: LOG (fluid width) | CLIENTS (fixed width). Grows once the mid
  band is satisfied, so tall terminals show more log lines + clients.

Power + signal live above the router ESSID (the live rainbow signal bar), not in
the top bar. Portrait is deferred.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from wifit3.campaigns import treelog
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.pmkid import PmkidHarvestAttack
from wifit3.campaigns.wep import WepCampaign
from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.campaigns.decloak import DecloakCampaign, DecloakInput, KnownDecloakCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign, EvilTwinInput
from wifit3.campaigns.pbc import WpsPbcCampaign
from wifit3.campaigns.pin import (EMPTY_PIN_LABEL, WpsCampaign, load_run_state,
                                  run_progress_line)
from wifit3.campaigns.probe import probe_ap
from wifit3.campaigns.probe.broadcom_wps import WpsImplementation, probe_wps_implementation
from wifit3.campaigns.verify import VERIFY_CAMPAIGNS
from wifit3.campaigns.verify.campaign import VerifyCampaign, VerifyOutcome
from wifit3.campaigns.wps.registrar import PinResult
from wifit3.crack.handshake import handshake_uncrackable_label
from wifit3.dot11.pmf import PmfPosture
from wifit3.models import CREDENTIAL_NAMES, AccessPoint, IdSource
from wifit3.models.access_point import eap_only, no_passphrase
from wifit3.persist.ap_state import Key, PasswordStatus, epoch_sort_key
from wifit3.persist.config import Config
from wifit3.persist.save import SaveResult
from wifit3.ui import focus_model as fm, pmkid_log
from wifit3.ui.capture_log import DECLOAK_METHOD_LABELS, saved_psk_chip, short_sta
from wifit3.ui.eapol_aggregate import EapolAggregator
from wifit3.ui.encryption_format import psk_capture_supported, wep_key_hex_and_ascii
from wifit3.ui.screens.focus_v2.eviltwin_modal import EvilTwinInputModal, _can_host
from wifit3.ui.screens.vault_item import key_text
from wifit3.ui.vault.global_tracker import GlobalJobTracker
from wifit3.watchers.ap_watcher import (
    ApDecloaked, ApEvent, EapolFrameSeen, HandshakeCaptured, PmkidCaptured,
)

from .campaign_controls import CampaignControls
from .card_endpoint import CardEndpoint
from .clients_list import ClientsList, ClientWidget, FingerprintModal
from .decloak_modal import DecloakInputModal
from .log_band import LogBand
from .router_endpoint import RouterEndpoint
from .tx_picker import TxDevicePicker
from .verify_menu import VerifyKeyMenu
from .packet_dashboard import PacketDashboard
from . import art

if TYPE_CHECKING:
    from wifit3.ui.app import WifiteApp

logger = logging.getLogger(__name__)

_ENDPOINT_W = 20  # the .ans art is exactly 20 cells wide
_TOPBAR_H = 3
_CHROME_H = 2  # Buffer for Textual's Header & Footer (1 row each)
_BORDER = "$primary"  # Border/title color for  LOG / CLIENTS panels
_STACKED_CLIENT_ROWS_MAX = 6

# Mid-band height
_CENTER_MAX = 13
_CENTER_MIN = 7
_COMPACT_CENTER_H = 7
_BOTTOM_MIN = 6
# Horizontal padding on the mid row
_PAD_START = 80
_PAD_RATE = 0.4

_PBC_RETRY_COOLDOWN_S = 3.0

_ATTACK_BUTTONS = [
    ("btn-decloak", "Odkryj"), ("btn-known-decloak", "Odkryj"),
    ("btn-gen-ivs", "ARP Replay"), ("btn-chop", "ChopChop"), ("btn-deauth", "AutoDeauth"),
    ("btn-pmkid", "PMKID"), ("btn-wps-pin", "WPS PIN"), ("btn-eviltwin", "EvilTwin"),
    ("btn-verify", "Weryfikuj"), ("btn-pbc", "Stop PBC"),
]

# Static button tooltips, keyed by live label so toggled buttons get idle/run-specific tips.
_BUTTON_TIPS = {
    "ARP Replay": "Nasłuchuje i odtwarza pakiety ARP",
    "Stop Replay": "Zatrzymuje całą kampanię WEP.",
    "ChopChop": "Wytwarza pakiet do replayu",
    "Stop Chop": "Przerywa ChopChop i wraca do ARP replay",
    "AutoDeauth": "Deautoryzuje klientów pojedynczo, potem broadcast. Zapętla.",
    "PMKID": "Łączy się, by wyłuskać PMKID\n(niektóre AP niepodatne)",
    "WPS PIN": "Ataki PIN: PixieDust, domyślne PINy producenta, potem brute-force",
    "EvilTwin": "Przepina klientów na bliźniaka WPA2, by przechwycić handshake do złamania",
    "Stop EvilTwin": "Zdejmuje bliźniaka i zwraca klientów do AP",
    "Weryfikuj": "Łączy się i weryfikuje zapisane dane",
    "Weryfikuj ▾": "Łączy się i wybiera dane do weryfikacji",
}


# Pretty-print a capture filename, skipping the <bssid>_<epoch> middle
_FILENAME_MIDDLE = re.compile(r"_[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5}_\d+_")


def _save_line(result: SaveResult) -> str:
    """Short, dim 'saved/exists: captures/<essid>_…_<kind>.<ext>' line for a save result."""
    verb = "zapisano" if result.was_new else "już zapisano"
    name = result.path.name
    m = _FILENAME_MIDDLE.search(name)
    short = f"{name[:m.start()]}_…_{name[m.end():]}" if m else name
    return f"[dim]{verb}: {escape(str(Path(Config.captures_dir) / short))}[/dim]"


def _wep_key_chip(key_hex) -> str:
    """Black-bold-on-cyan WEP key chip (bare hex for non-printable keys)."""
    if not key_hex:
        return "[dim]?[/dim]"
    return f"[black bold on cyan] {wep_key_hex_and_ascii(key_hex)} [/black bold on cyan]"


class FocusViewV2(Screen):
    app: "WifiteApp"

    HORIZONTAL_BREAKPOINTS = [
        (0, "-tiny"), (60, "-narrow"), (81, "-compact"),
        (100, "-normal"), (140, "-wide"),
    ]
    VERTICAL_BREAKPOINTS = [(0, "-compact-height"), (32, "-short"), (36, "-tall")]

    # Attack hotkeys come from the campaign registry
    BINDINGS = [
        Binding("escape", "go_back", "Wróć", show=True),
        Binding("d", "deauth_all", "Deauth", show=True),
        *[Binding(cls.hotkey[0], f"campaign('{cls.key}')", cls.hotkey[1], show=True)
          for cls in fm.BUTTON_CAMPAIGNS if cls.hotkey],
        Binding("c", "campaign('chop')", "ChopChop", show=True),
        Binding("w", "wps_pbc_mode", "WPS PBC", show=True),
        Binding("s", "silence", "Wycisz", show=True),
        Binding("s", "unsilence", "Odcisz", show=True),
        Binding("q", "app.quit", "Wyjdź", show=True),
    ]

    _DEAUTH_SEL_ROUNDS = 10
    _DEAUTH_BCAST_COUNT = 20

    CSS = """
    FocusViewV2 { layout: vertical; background: $surface; overflow: hidden; }

    #topbar { height: %(top)d; }
    #actions { width: auto; height: 100%%; }
    #topbar Button { height: 3; width: auto; min-width: 0; margin: 0 1 0 0; }
    FocusViewV2.-compact-height #actions { align: left middle; }
    FocusViewV2.-compact-height #topbar Button { height: 1; border: none; }
    /* No background override on .attack-btn */
    #status { width: 1fr; height: 3; content-align: center middle; text-align: center; }
    #rspacer { width: 0; height: 1; }

    #mid { height: 1fr; }
    #card, #router { width: %(ew)d; align: center middle; }
    #card { dock: left; }
    /* Router art is a row shorter than the card's, so bottom-align it. */
    #router { dock: right; align: center bottom; }
    FocusViewV2.-tiny #mid { layers: graph router card; }
    FocusViewV2.-tiny #dashboard {
        layer: graph;
        width: 1fr;
        margin-right: %(ew)d;
    }
    FocusViewV2.-tiny #router {
        layer: router;
        dock: right;
        height: 50%%;
    }
    FocusViewV2.-tiny #card {
        layer: card;
        dock: right;
        height: 50%%;
        offset-y: 50%%;
    }
    FocusViewV2.-tiny.-short #card,
    FocusViewV2.-tiny.-compact-height #card {
        display: block;
        dock: right;
    }
    #dashboard { width: 1fr; height: 100%%; padding: 0 1; }
    .endpoint-art { width: %(ew)d; background: transparent; }
    FocusViewV2.-tiny .endpoint-art,
    FocusViewV2.-short .endpoint-art,
    FocusViewV2.-compact-height .endpoint-art { display: none; }
    FocusViewV2.-short #card,
    FocusViewV2.-compact-height #card {
        height: 3;
        offset-y: 4;
        align: center top;
    }
    FocusViewV2.-short #router,
    FocusViewV2.-compact-height #router {
        height: 5;
        offset-y: 0;
        align: center top;
    }
    FocusViewV2.-short #ap-power,
    FocusViewV2.-compact-height #ap-power { margin-bottom: 1; }
    FocusViewV2.-tiny.-short #router,
    FocusViewV2.-tiny.-compact-height #router { height: 4; }
    FocusViewV2.-tiny.-short #ap-power,
    FocusViewV2.-tiny.-compact-height #ap-power { margin-bottom: 0; }

    .card-static, .ap-static { width: 100%%; height: 1; text-align: center; color: $text-muted; }
    .card-dynamic { width: 100%%; height: 1; text-align: center; color: $accent; }
    .ap-essid { width: 100%%; height: 1; text-align: center; text-style: bold; }
    .ap-power { width: 100%%; height: 1; text-align: center; }
    #ap-identity-row { width: 100%%; height: 1; align-horizontal: center; }
    #ap-identity {
        width: 1fr; height: 1; min-width: 0; border: none; margin: 0; padding: 0;
        background: transparent; color: $text-muted; content-align: center middle;
    }
    #ap-identity.identity-known { text-style: underline; color: $secondary; }
    #ap-identity:focus { text-style: bold reverse; }
    #ap-probe {
        width: 4; height: 1; min-width: 0; border: none; margin: 0; padding: 0;
        background: transparent;
    }
    #ap-probe:hover { background: $surface-lighten-1; }
    #ap-probe:focus { text-style: bold reverse; }

    #bottom { height: 1fr; }
    #log { width: 1fr; height: 100%%; border: round %(border)s;
           border-title-color: %(border)s; border-title-style: bold; padding: 0 1; }
    #log-rich { width: 100%%; height: 1fr; background: transparent; border: none; padding: 0; }
    #clients { dock: right; width: 40; height: 100%%; border: round %(border)s;
               border-title-color: %(border)s; border-title-style: bold; padding: 0 1; }
    FocusViewV2.-tiny #bottom, FocusViewV2.-narrow #bottom { layout: vertical; }
    FocusViewV2.-tiny #clients, FocusViewV2.-narrow #clients {
        dock: none; width: 100%%; height: auto;
    }
    FocusViewV2.-tiny #client-rows, FocusViewV2.-narrow #client-rows {
        height: auto; min-height: 1; max-height: %(stacked_client_rows_max)d;
    }
    FocusViewV2.-tiny #log, FocusViewV2.-narrow #log { width: 100%%; height: 1fr; }
    /* Rows scroll inside a fixed-height region; the broadcast button stays pinned. */
    #client-rows { width: 100%%; height: 1fr; }

    .bcast-btn { width: 100%%; height: 1; min-width: 0; border: none; margin: 0 0 1 0;
                 background: $error; color: $text; content-align: center middle; }
    .client-row { height: 1; width: 100%%; }
    .cl-fp { width: 2; }
    /* A known fingerprint is clickable (pops up the detail popup): underline + accent color on
       the MAC marks it, same as any other actionable text. Not on the emoji itself -- the
       underline renders through the glyph rather than under it, which reads as broken/ugly. */
    .cl-bssid.fp-known { text-style: underline; color: $secondary; }
    .cl-bssid { width: 17; }
    .cl-pwr { width: 5; text-align: right; }
    .cl-pkts { width: 6; text-align: right; }
    .cl-deauth { width: 3; min-width: 3; height: 1; border: none; margin: 0 0 0 1;
                 background: red; color: white; content-align: center middle; }
    """ % {
        "stacked_client_rows_max": _STACKED_CLIENT_ROWS_MAX,
        "ew": _ENDPOINT_W,
        "top": _TOPBAR_H,
        "border": _BORDER,
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._target_ap = None
        self._last_status: list[str] | None = None   # last-pushed headline; skip no-op repaints
        self._beacon_samples: deque = deque()
        self._eapol_agg = EapolAggregator(settle_s=3.0)
        self._tick_timer = None
        # The one running campaign (Campaign.active is the radio mutex).
        self._controls = CampaignControls()
        self._verify_key: Optional[Key] = None     # the credential the running Verify checks
        self._pbc_user_stopped = False
        self._pbc_retry_after = 0.0   # monotonic time before which we won't re-arm a PBC retry
        self._probe_task: Optional[asyncio.Task] = None
        self._prev_stats = None
        self._campaign_toggles = {
            "wep": self._toggle_generate_ivs, "pmkid": self._toggle_pmkid,
            "wps": self._toggle_wps_pin, "chop": self._toggle_chop,
            "deauth": self._toggle_deauth, "decloak": self._toggle_decloak,
        }
        self._binding_sig: Optional[tuple] = None
        self._rspacer_w = -1                          # last-set spacer width; skip no-op relayouts
        self._status_below_actions = False
        self._status_layout_inputs: tuple = ()
        self._last_footer_markups: tuple[str, ...] | None = None

    # ----- compose -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="topbar"):
            with Horizontal(id="actions"):
                yield Button("‹ Skaner", id="back")
                # The full attack set is composed once (hidden); refresh_buttons shows the ones that fit the target.
                for bid, label in _ATTACK_BUTTONS:
                    btn = Button(label, id=bid, classes="attack-btn")
                    btn.display = False
                    yield btn
            status = self._status()
            self._last_status = status
            yield Static(self._render_status(status), id="status")
            # Right spacer to accurately align status to sparklines.
            yield Static("", id="rspacer")
        with Horizontal(id="mid") as mid:
            mid.ALLOW_SELECT = False
            yield PacketDashboard(self._dashboard_rows(), id="dashboard")
            yield CardEndpoint(**self._card_values(), id="card")
            yield RouterEndpoint(**self._router_values(), id="router")
        with Horizontal(id="bottom"):
            yield ClientsList(self._client_list(), id="clients")
            yield LogBand([], id="log")
        yield GlobalJobTracker()
        yield Footer()

    async def on_mount(self) -> None:
        self._tick_timer = self.set_interval(1 / 10, self._tick)
        if hasattr(self.app, "active_jobs"):
            self.watch(self.app, "active_jobs", self._redistribute_after_tracker_update)
        if hasattr(self.app, "vault_open"):
            self.watch(self.app, "vault_open", self._redistribute_after_tracker_update)
        self._distribute()
        await self._enter_target()

    def _redistribute_after_tracker_update(self, *_args) -> None:
        self.call_after_refresh(self._distribute)

    async def on_screen_resume(self) -> None:
        # Full re-acquire only on a target change; a same-target return keeps the live view.
        target = self.app.target_ap
        if target is not self._target_ap:
            await self._enter_target()
        elif target is not None and self.app.array is not None:
            self.run_worker(self._repin_to_target(self.app.array, target), group="tune", exclusive=True)

    async def _tune_to_target(self, array, ap: AccessPoint) -> None:
        await array.stop_hopping()
        try:
            ok = await array.set_channel(ap.channel, scan=False)
        except Exception:
            logger.exception("Focus v2 channel tune failed")
            ok = False
        if ok:
            self._log(f"Ustawiono [cyan]kanał {ap.channel}[/cyan]")
        else:
            self._log(f"[yellow]Próba ustawienia kanału {ap.channel}[/yellow]")

    async def _repin_to_target(self, array, target: AccessPoint) -> None:
        await array.stop_hopping()
        # Re-pin the pool to the target's channel on re-entry (STACK across channel-capable cards).
        # Not while a campaign runs: it owns its cards' channels (EvilTwin's twin sits off-target).
        if self._any_campaign_active():
            return
        ok = await array.set_channel(target.channel, scan=False)
        logger.info("[FOCUS] re-pin: bssid=%s ch=%s -> %s", target.bssid, target.channel, ok)

    def on_resize(self) -> None:
        self._distribute()

    # ----- snapshot building -------------------------------------------------

    def _pbc_busy(self) -> bool:
        cur = self._controls.current
        return isinstance(cur, WpsPbcCampaign) and not cur.done

    def _is_probing(self) -> bool:
        return self._probe_task is not None and not self._probe_task.done()

    def _any_campaign_active(self) -> bool:
        return Campaign.active is not None or self._controls.current is not None

    def _stop_probe(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
        self._probe_task = None

    def _router_values(self) -> dict:
        """The AP identity + power primitives the router endpoint renders, read live from
        the target (blanks when there's no target). ``beacon_rate`` mutates the beacon
        deque, so this is its single caller per tick."""
        ap = self.app.target_ap
        if ap is None:
            return dict(essid="", bssid="", channel=0, power_dbm=-100, signal=None,
                        wps=False, has_m1=False, probing=False, probe_disabled=False,
                        identity="", identity_details=None)
        if ap.ssid:
            essid = fm.truncate_ssid(ap.ssid)
        else:
            essid = "‹ukryta›"
            guessid = self.app.guessid(ap)
            if guessid:
                essid = f"{fm.truncate_ssid(guessid)}?"
        rate, _ = fm.beacon_rate(ap, self._beacon_samples, time.time())
        return dict(essid=essid, bssid=ap.bssid, channel=ap.channel,
                    power_dbm=ap.signal, signal=rate,
                    wps=bool(ap.wps),
                    has_m1=ap.identity.has_source(IdSource.WSC_M1),
                    probing=self._is_probing(),
                    probe_disabled=self._any_campaign_active(),
                    identity=ap.identity.summary,
                    identity_details=fm.router_identity_details(ap))

    def _card_values(self) -> dict:
        """The card endpoint's compose seed: chipset + own MAC from the live pool, plus the
        current dynamic line. Identity then tracks the pool live via ``_sync_card``."""
        chipset, bssid = fm.card_identity(self.app.array)
        return dict(chipset=chipset, bssid=bssid, dynamic=fm.status_under_card())

    def _status(self) -> list[str]:
        """The headline lines for the live target (empty when there's no target)."""
        ap = self.app.target_ap
        if ap is None:
            return []
        return fm.status_headlines(ap, self.app.array, self.app.vault)

    def _dashboard_rows(self) -> list:
        """The packet-dashboard row set for the live target's encryption family (empty when
        there's no target). The widget samples the live counters itself."""
        ap = self.app.target_ap
        return fm.dashboard_rows(ap) if ap is not None else []

    def _client_list(self) -> list:
        """The live target's clients. Forged/own MACs are already excluded by the sink;
        other APs' clients are filtered here by BSSID. Empty when there's no target."""
        ap = self.app.target_ap
        array = self.app.array
        if ap is None or array is None:
            return []
        return [c for c in array.clients.values() if c.bssid == ap.bssid]

    @staticmethod
    def _render_status(status) -> Text:
        return Text("\n").join(Text.from_markup(s, emoji=False) for s in status)

    def _sync_card(self) -> None:
        """Refresh the card endpoint (picker + art) from the live pool, polled because WlanArray has
        no arrival callback. The shown card is the TX card: the campaign's locked one, else
        select_iface's pick for this target."""
        array = self.app.array
        members = array.members if array else []
        active = Campaign.active
        ap = self.app.target_ap
        if active is not None:
            primary = active.iface
        elif ap is not None and array is not None:
            primary = array.select_iface(ap.channel)
        else:
            primary = None
        primary = primary or art.pick_primary(members)   # no capable card: still show something
        card = self.query_one("#card", CardEndpoint)
        card.set_art(art.art_path_for(primary) if primary is not None else art.pool_art(members))
        card.sync_picker(members, ap.channel if ap is not None else None,
                         primary, active is not None)
        card.update_bssid(members[0].mac_address if len(members) == 1 else None)

    def on_tx_device_picker_selected(self, event: TxDevicePicker.Selected) -> None:
        """User pinned a TX card in the picker: record the preference and sync the endpoint."""
        if self.app.array is not None:
            self.app.array.prefer(event.iface)
        self._sync_card()

    def refresh_buttons(self) -> None:
        """Set each attack button straight from its campaign class + the active campaign.
        No ButtonState in between: read the campaign, write the widget."""
        ap = self.app.target_ap
        if ap is None:
            return
        active = Campaign.active
        probing = self._is_probing()
        for cls in fm.BUTTON_CAMPAIGNS:
            btn = self.query_one(f"#{cls.button_id}", Button)
            btn.display = cls.visible(ap)
            running = active is not None and active.key == cls.key and cls.stoppable
            if running:
                btn.label, btn.variant, btn.disabled, reason = cls.run_label, cls.run_variant, False, ""
            else:
                reason = fm.campaign_blocked(cls, ap)
                btn.label, btn.variant = Content(cls.button_label(ap)), cls.idle_variant
                btn.disabled = reason is not None
            if probing and not running:
                btn.disabled, reason = True, "Zablokowane na czas sondy"
            btn.tooltip = reason or _BUTTON_TIPS.get(str(btn.label))
        self._refresh_chop_button(ap, active, probing)
        self._refresh_pbc_button(ap, probing)
        self._refresh_verify_button(ap, probing)

    def _refresh_chop_button(self, ap, active, probing: bool) -> None:
        """ChopChop: a WEP sub-action, enabled only while the WEP campaign runs."""
        btn = self.query_one("#btn-chop", Button)
        wep_running = active is not None and active.key == "wep"
        chopping = wep_running and getattr(active, "chop_active", False)
        btn.display = WepCampaign.visible(ap)
        btn.disabled = not wep_running or Config.is_silenced(ap.bssid) or probing
        btn.label = "Stop Chop" if chopping else "ChopChop"
        btn.variant = "warning" if chopping else "primary"
        btn.tooltip = _BUTTON_TIPS.get(str(btn.label))

    def _refresh_pbc_button(self, ap, probing: bool) -> None:
        """Stop PBC while it runs; Start PBC while the target's window is open and it doesn't."""
        btn = self.query_one("#btn-pbc", Button)
        if self._pbc_busy():
            stopping = getattr(self._controls.current, "stopped", False)
            btn.display = True
            btn.disabled = stopping or probing         # already draining → no double-stop
            btn.variant = "error"
            btn.label = "Zatrzymuję…" if stopping else "Stop PBC"
        elif ap.wps_pbc_active:
            reason = fm.campaign_blocked(WpsPbcCampaign, ap)
            btn.display = True
            btn.disabled = reason is not None or probing
            btn.variant = "primary"
            btn.label = "Start PBC"
            btn.tooltip = reason
        else:
            btn.display = False

    def _verify_keys(self, ap) -> list[Key]:
        """The target's stored credentials, newest first."""
        return sorted(self.app.vault.ap_state(ap.bssid).keys,
                      key=lambda key: epoch_sort_key(key.newest_capture.timestamp), reverse=True)

    def _refresh_verify_button(self, ap, probing: bool) -> None:
        """Shown while the Vault holds a credential for the target; ``Verify ▾`` picks one of several."""
        btn = self.query_one("#btn-verify", Button)
        keys = self._verify_keys(ap)
        btn.display = bool(keys)
        current = self._controls.current
        if isinstance(current, VerifyCampaign):
            btn.label = "Weryfikuję…"
            btn.disabled = True
            btn.variant = "warning"
            reason = None
        else:
            btn.label = "Weryfikuj ▾" if len(keys) > 1 else "Weryfikuj"
            btn.variant = "success"
            reason = fm.campaign_blocked(VerifyCampaign, ap)
            if probing:
                reason = "Zablokowane na czas sondy"
            btn.disabled = reason is not None or current is not None
        btn.tooltip = reason or _BUTTON_TIPS.get(str(btn.label))

    # ----- target (re)acquisition --------------------------------------------

    async def _enter_target(self) -> None:
        """Bind to ``app.target_ap``: stop campaigns, reset state, update panels/radio/log."""
        campaign = self._controls.current
        self._controls.stop()
        if campaign is not None:
            await campaign.stop()      # let its teardown finish before we retune for the new target
        self._stop_probe()
        self._pbc_user_stopped = False
        self._pbc_retry_after = 0.0

        ap = self.app.target_ap
        self._target_ap = ap
        if ap is None:
            return
        array = self.app.array
        logger.info("[FOCUS] enter: ssid=%r bssid=%s ch=%s", ap.ssid, ap.bssid, ap.channel)

        self._beacon_samples.clear()
        self._eapol_agg.reset()
        self._prev_stats = None        # drop the old target's counters
        self._last_footer_markups = None
        self.query_one("#log", LogBand).clear()

        status = self._status()
        self._last_status = status
        self.query_one("#status", Static).update(self._render_status(status))
        self.query_one("#dashboard", PacketDashboard).reconfigure(self._dashboard_rows(), array, ap.bssid)
        self.query_one("#card", CardEndpoint).update(dynamic=fm.status_under_card())
        self._sync_card()
        self.query_one("#router", RouterEndpoint).update(**self._router_values())
        self.query_one("#clients", ClientsList).sync(self._client_list())
        self.refresh_buttons()
        self._balance_status()
        self._refresh_status_footer()  # dashboard footer (cleared by reconfigure)

        # Log initial "target acquired"
        enc = fm.encryption_chip(ap)
        guessid = self.app.guessid(ap)
        if ap.ssid:
            chip = f"[black bold on cyan] {escape(ap.ssid)} [/black bold on cyan]"
            self._log(f"[bold]Cel namierzony:[/bold] {chip}")
        elif guessid:
            self._log("[bold]Cel namierzony:[/bold] "
                      f"[dim italic]ukryta sieć, prawdopodobnie {escape(guessid)}?[/dim italic]")
        else:
            self._log("[bold]Cel namierzony:[/bold] "
                      "[dim italic]ukryta sieć (ukryty SSID)[/dim italic]")
        self._log(treelog.branch(f"[dim]Szyfrowanie:[/dim] {enc}"))
        self._log(treelog.branch(f"[dim]BSSID:[/dim] {ap.bssid}"))
        if array:
            self._log(treelog.leaf(f"Ustawiam [cyan]kanał {ap.channel}[/cyan]…"))
            self.run_worker(self._tune_to_target(array, ap), group="tune", exclusive=True)
        else:
            self._log(treelog.leaf("[yellow]brak interfejsu: tylko podgląd[/yellow]"))

        if ap.pmf_posture is PmfPosture.REQUIRED:
            self._log("[bold yellow]PMF wymagane:[/] "
                      "AP wymaga [bold]chronionych ramek zarządzania[/]")
            self._log(treelog.leaf("Ataki [italic]deauth[/] zostały wyłączone"))
        elif ap.pmf_posture is PmfPosture.INVALID:
            self._log("[bold yellow]PMF błędne:[/] sprzeczne ogłoszenie MFPC/MFPR")
            self._log(treelog.leaf("[italic]Deauth[/] wyłączone, bo ochrona nieznana"))

        self._log_persisted_history(ap)

        if Config.is_silenced(ap.bssid):
            self._log_silenced()
            return

        enc = (ap.encryption or "").upper()
        if enc == "WEP":
            self._log("[bold italic]Pasywny nasłuch[/bold italic] [bold]WEP IVs[/bold]")
        elif enc in ("OPEN", ""):
            pass
        elif eap_only(ap):
            self._log("[dim]802.1X Enterprise: jeszcze niewspierane[/dim]")
        elif ap.wpa3 and not ap.transition_mode:
            self._log("[dim]WPA3/SAE: przechwyt pasywny niemożliwy[/dim]")
        elif no_passphrase(ap):
            self._log("[dim]OWE (Enhanced Open): klucz Diffie-Hellman, nie ma czego łamać[/dim]")
        elif ap.akm_suites and psk_capture_supported(ap) is False:
            self._log("[dim]Jeszcze niewspierane: brak ścieżki łamania dla tego AKM[/dim]")
        else:
            self._log("[bold italic]Pasywny nasłuch[/bold italic]:")
            self._log(treelog.branch("[bold]Handshake'i[/bold] 4-Way do złamania"))
            self._log(treelog.leaf("[bold]PMKID-y[/bold] do złamania"))

    def _log_persisted_history(self, ap) -> None:
        """On focus init, print captures/ artifacts for this AP to the log."""
        wps_state = load_run_state(Config.captures_dir, ap.bssid)
        wps_progress = run_progress_line(wps_state) if wps_state else None
        rows = list(self.app.vault.detailed_summary(ap).items())
        if not rows and not wps_progress:
            return
        if rows:
            self._log("[bold]Istniejące przechwyty[/bold] w [cyan]captures/[/cyan]:")
        # Pad the label column so the dates line up.
        label_w = max((len(f"{label} ({n})") for label, (_cap, n) in rows), default=0)
        for i, (label, (cap, n)) in enumerate(rows):
            # The WPS progress leaf, if present, takes the └, so a saved row is the last leaf only when nothing follows.
            last = i == len(rows) - 1 and wps_progress is None
            line = treelog.leaf if last else treelog.branch
            pad = " " * (label_w - len(f"{label} ({n})"))
            head = f"[bold cyan]{label}[/bold cyan] [dim]({n})[/dim]{pad}"
            dt = datetime.fromtimestamp(cap.timestamp)
            if label == "WEP Key":
                self._log(line(f"{head}  {_wep_key_chip(cap.value)} "
                               f"[dim]{dt:%Y-%m-%d %H:%M}[/dim]"))
            elif label in ("WPS PIN", "WPS PBC"):
                creds = []
                if cap.value:
                    creds.append(f"[black bold on cyan] {escape(cap.value)} [/black bold on cyan]")
                if cap.pin is not None:
                    creds.append(f"[dim]PIN[/dim] {escape(cap.pin or EMPTY_PIN_LABEL)}")
                joined = ("  ".join(creds) + "  ") if creds else ""
                self._log(line(f"{head}  {joined}[dim]{dt:%Y-%m-%d %H:%M}[/dim]"))
            else:
                self._log(line(f"{head}  {dt:%Y-%m-%d} "
                               f"[dim]{dt:%H:%M}[/dim]"))
        if wps_progress is not None:
            self._log(treelog.leaf(wps_progress) if rows else wps_progress)

    # ----- per-tick paint ----------------------------------------------------

    def _tick(self) -> None:
        if self._target_ap is None or not self.is_current:
            return
        ap = self._target_ap

        # WEP recovers its key while still running; WPS reaches a terminal phase while its
        # task drains. Ask them to stop so they reap (below) on a following tick.
        cur = self._controls.current
        if cur is not None and cur.check_auto_stop():
            self._controls.request_stop()
        elif (cur is not None and not isinstance(cur, WpsPbcCampaign) and not cur.stopped
              and self._target_wants_pbc(ap)):
            self._log(f"[bold cyan]WPS PBC[/bold cyan]: okno otwarte, zatrzymuję {cur.idle_label}")
            self._controls.request_stop()

        # Reap a finished campaign: log/save its result, free the slot.
        finished = self._controls.reap()
        if finished is not None:
            handler = getattr(self, f"_finish_{finished.key}", None)
            if handler:
                handler(finished)
        # Clear the manual-stop suppression when the window closes; a fresh one re-arms.
        if not ap.wps_pbc_active:
            self._pbc_user_stopped = False
        if self._should_auto_invade_pbc(ap):
            self._start_pbc_capture(ap)

        status = self._status()
        if status != self._last_status:
            self._last_status = status
            self.query_one("#status", Static).update(self._render_status(status))
        self.query_one("#card", CardEndpoint).update(dynamic=fm.status_under_card())
        self._sync_card()
        self.query_one("#router", RouterEndpoint).update(**self._router_values())
        clients = self.query_one("#clients", ClientsList)
        clients.sync(self._client_list())
        clients.set_deauth_enabled(not fm.deauth_blocked(ap))
        self.refresh_buttons()
        self._balance_status()
        self._sync_bindings()
        self._refresh_status_footer()
        array = self.app.array
        self._drive_leds(ap, array)
        self._flush_eapol_bursts(ap)

    def _target_wants_pbc(self, ap) -> bool:
        if not ap.wps_pbc_active or self.app.pbc_ignore_reasons(ap):
            return False
        return not self._pbc_user_stopped

    def _should_auto_invade_pbc(self, ap) -> bool:
        if not self._target_wants_pbc(ap) or time.monotonic() < self._pbc_retry_after:
            return False
        return self._controls.current is None   # no other attack owns the radio

    def _distribute(self) -> None:
        """Compact the graph below 36 rows and give the reclaimed space to the bottom panels."""
        tracker = self.query_one(GlobalJobTracker)
        self._balance_status()
        topbar_height = self._topbar_height()
        chrome = _CHROME_H + int(bool(tracker.display))
        avail = max(1, self.size.height - topbar_height - chrome)
        if not self.has_class("-tall"):
            center = _COMPACT_CENTER_H
        else:
            center = min(_CENTER_MAX, max(_CENTER_MIN, avail - _BOTTOM_MIN))
        center = max(1, min(center, avail - 1))
        mid = self.query_one("#mid")
        mid.styles.height = center
        self.query_one("#bottom").styles.height = avail - center
        pad = max(0, round((self.size.width - _PAD_START) * _PAD_RATE))
        mid.styles.padding = (0, pad, 0, pad)

    def _topbar_height(self) -> int:
        if not self._status_below_actions:
            return _TOPBAR_H
        return (1 if self.has_class("-compact-height") else _TOPBAR_H) + _TOPBAR_H

    def _balance_status(self) -> None:
        """Center status beside actions, or place it below when symmetry no longer fits."""
        topbar = self.query_one("#topbar")
        topbar_w = self.size.width
        actions = self.query_one("#actions")
        visible_buttons = [button for button in actions.query(Button) if button.display]
        widest = max((Text.from_markup(s, emoji=False).cell_len
                      for s in (self._last_status or [])), default=0)
        inputs = (topbar_w, widest, self.has_class("-compact-height"),
                  tuple((str(button.label), tuple(button.classes)) for button in visible_buttons))
        if inputs == self._status_layout_inputs:
            return
        self._status_layout_inputs = inputs
        actions_w = sum(
            button.get_content_width(self.size, self.size) + button.styles.margin.width
            for button in visible_buttons
        )
        status_below = 2 * actions_w + widest > topbar_w
        layout_changed = self._status_below_actions != status_below
        self._status_below_actions = status_below
        topbar.styles.layout = "vertical" if status_below else "horizontal"
        topbar.styles.height = self._topbar_height()
        actions.styles.width = "100%" if status_below else "auto"
        action_height = 1 if self.has_class("-compact-height") else _TOPBAR_H
        actions.styles.height = action_height if status_below else "100%"
        status = self.query_one("#status", Static)
        status.styles.width = "100%" if status_below else "1fr"
        self.query_one("#rspacer", Static).display = not status_below
        if layout_changed:
            self.call_after_refresh(self._distribute)
        spacer = 0 if status_below else actions_w
        if spacer != self._rspacer_w:
            self._rspacer_w = spacer
            self.query_one("#rspacer", Static).styles.width = spacer

    def _refresh_status_footer(self) -> None:
        """Refresh status footer under sparklines."""
        ap = self._target_ap
        if ap is None:
            return
        array = self.app.array
        markups = tuple(fm.status_under_dash(ap, array, time.time()))
        if markups == self._last_footer_markups:
            return
        self._last_footer_markups = markups
        lines = [Text.from_markup(markup, emoji=False) for markup in markups]
        self.query_one("#dashboard", PacketDashboard).set_footer(lines)

    # ----- endpoint LED flicker (instrumentation) ----------------------------

    # Router LED = RX from the target; card LED = TX we send.
    _RX_KEYS = ("beacon", "data", "eapol", "wep_iv")
    _TX_KEYS = ("inject", "deauth")

    def _drive_leds(self, ap, array) -> None:
        """Flicker the endpoint LEDs on real traffic."""
        if array is None:
            return
        snap = array.packet_stats.snapshot(ap.bssid)
        prev, self._prev_stats = self._prev_stats, snap
        if prev is None:   # first frame after (re)acquire, no delta yet
            return
        if any(snap.get(k, 0) > prev.get(k, 0) for k in self._RX_KEYS):
            self.query_one("#router", RouterEndpoint).flicker()
        if any(snap.get(k, 0) > prev.get(k, 0) for k in self._TX_KEYS):
            self.query_one("#card", CardEndpoint).flicker()

    # ----- event log (capture pipeline) --------------------------------------

    def on_wifite_app_ap_event_detected(self, message: "WifiteApp.ApEventDetected") -> None:
        # EAPOL + handshake completions go through the aggregator (one tree per client); PMKID / decloak are immediate.
        ev = message.event
        if self._target_ap is None or ev.bssid != self._target_ap.bssid:
            return
        if isinstance(ev, EapolFrameSeen):
            self._eapol_agg.on_eapol(ev, time.time())
        elif isinstance(ev, HandshakeCaptured):
            result = ev.save_result
            hint = _save_line(result) if result is not None else None
            chip = saved_psk_chip(result.matches_saved_psk) if result is not None else None
            self._emit_lines(self._eapol_agg.on_handshake(ev, save_hint=hint, psk_chip=chip))
        else:
            self._log_capture_event(ev)

    def _flush_eapol_bursts(self, ap) -> None:
        # Label real-but-uncrackable bursts.
        for lines in self._eapol_agg.tick(time.time(), label_for=lambda mac: self._uncrackable_reason(ap, mac)):
            self._emit_lines(lines)

    def _emit_lines(self, lines) -> None:
        for ln in lines:
            self._log(ln)

    def _uncrackable_reason(self, ap, mac: str) -> str | None:
        """The uncrackable-AKM badge (SAE/FT/EAP/OWE) for a client's handshake, else None."""
        hs = ap.handshakes.get(mac)
        return handshake_uncrackable_label(hs) if hs is not None else None

    def _log_capture_event(self, ev: ApEvent) -> None:
        if isinstance(ev, PmkidCaptured):
            self._log(
                f"[black bold on green] ✓ PMKID przechwycony [/black bold on green] "
                f"od [bold]{short_sta(ev.client_mac)}[/bold]"
            )
            result = ev.save_result
            if result is not None:
                chip = saved_psk_chip(result.matches_saved_psk)
                if chip:
                    self._log(treelog.branch(chip))
                self._log(treelog.leaf(_save_line(result)))
        elif isinstance(ev, ApDecloaked):
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            self._log(
                f"[bold]Odkryto[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]przez {method_label}[/dim]"
            )

    def on_wifite_app_job_cracked(self, message: "WifiteApp.JobCracked") -> None:
        job = message.job
        if self._target_ap is None or self.app.vault.manager.job_bssid(job) != self._target_ap.bssid:
            return
        status = self.app.vault.manager.key_status(job) or PasswordStatus.CURRENT
        if status is PasswordStatus.CURRENT:
            chip, label = "black bold on green", ""
        else:
            chip, label = "black bold on yellow", f" [bold yellow]{status.value}[/bold yellow]"
        self._log(
            f'[{chip}] ✓ PSK złamany [/{chip}] '
            f'"{escape(job.key or "")}"{label} [dim](przez {escape(job.display_name)})[/dim]'
        )

    def _log(self, markup: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#log", LogBand)
        except Exception:
            return
        log.write(Text.from_markup(f"[dim]{ts}[/dim]  {markup}", emoji=False))

    # ----- button dispatch ---------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "back":
            await self.action_go_back()
        elif bid == "deauth-all":
            self.run_worker(self._run_deauth_broadcast(), exclusive=True)
        elif bid == "btn-deauth":
            self._toggle_deauth()
        elif bid == "btn-pmkid":
            self._toggle_pmkid()
        elif bid == "btn-decloak":
            self._toggle_decloak()
        elif bid == "btn-known-decloak":
            self._toggle_known_decloak()
        elif bid == "btn-wps-pin":
            self._toggle_wps_pin()
        elif bid == "btn-eviltwin":
            self._toggle_eviltwin()
        elif bid == "btn-gen-ivs":
            self._toggle_generate_ivs()
        elif bid == "btn-chop":
            self._toggle_chop()
        elif bid == "btn-pbc":
            self._toggle_pbc()
        elif bid == "btn-verify":
            self._verify_pressed(event.button)

    def on_client_widget_deauth_requested(self, event: ClientWidget.DeauthRequested) -> None:
        self.run_worker(self._run_deauth_selected(event.mac), exclusive=True)

    def on_client_widget_fingerprint_clicked(self, event: ClientWidget.FingerprintClicked) -> None:
        self.app.push_screen(FingerprintModal(event.mac, event.fingerprint, offset=event.offset))

    def on_router_endpoint_identity_requested(self, event: RouterEndpoint.IdentityRequested) -> None:
        self._log_identity_details_text(event.details)

    def on_router_endpoint_probe_requested(self, event: RouterEndpoint.ProbeRequested) -> None:
        ap = self._target_ap
        if ap is None or not ap.wps:
            return
        if self._is_probing():
            return
        if self._any_campaign_active():
            self._log(treelog.leaf_fail("sonda zablokowana podczas ataków"))
            return
        self._probe_task = asyncio.create_task(self._run_probe(ap))
        self.refresh_buttons()
        self._sync_bindings()
        self.query_one("#router", RouterEndpoint).update(**self._router_values())

    def on_router_endpoint_probe_cancel_requested(self, event: RouterEndpoint.ProbeCancelRequested) -> None:
        self._stop_probe()

    async def _run_probe(self, ap: AccessPoint) -> None:
        array = self.app.array
        if not array:
            self._log(treelog.leaf_fail("brak aktywnego interfejsu"))
            return
        iface = array.select_iface(ap.channel)
        if iface is None:
            self._log(treelog.leaf_fail(f"brak interfejsu dla CH {ap.channel}"))
            return
        label = escape(ap.ssid or ap.bssid)
        self._log(treelog.header(
            f"[bold]Sonda tożsamości[/bold] dla [cyan]{label}[/cyan] [dim](CH {ap.channel})[/dim]"
        ))
        try:
            async with array.claim(iface):
                self._log_wps_implementation(await probe_wps_implementation(iface, ap))
                result = await probe_ap(iface, ap)
                if result.ok:
                    self._log(treelog.leaf_ok(f"sonda zgodna: [bold]{escape(ap.identity.summary)}[/bold]"))
                    self._log_identity_details(ap)
                else:
                    self._log(treelog.leaf_fail(
                        f"sonda tożsamości nieudana [dim]({escape(result.detail or 'brak opisu')})[/dim]"
                    ))
        except asyncio.CancelledError:
            self._log(treelog.leaf_fail("sonda tożsamości anulowana"))
            raise
        except Exception as exc:
            logger.exception("Active probe failed")
            self._log(treelog.leaf_fail(f"błąd sondy tożsamości: {escape(str(exc))}"))
        finally:
            self._probe_task = None
            try:
                self.refresh_buttons()
                self._sync_bindings()
                self.query_one("#router", RouterEndpoint).update(**self._router_values())
            except Exception:
                pass

    def _log_wps_implementation(self, implementation: WpsImplementation) -> None:
        if implementation is WpsImplementation.BROADCOM_WPS_MONITOR:
            self._log(treelog.branch(
                "[cyan bold]broadcom-wps:[/cyan bold] [black bold on green] może być podatny [/black bold on green]"
            ))
            self._log(treelog.branch("uruchamia Broadcom wps_monitor"))
            self._log(treelog.branch(
                "[link=https://github.com/derv82/wifit3/discussions/135][cyan]derv82/wifit3 discussion#135[/cyan][/link]"
            ))
        elif implementation is WpsImplementation.HOSTAPD:
            self._log(treelog.branch("[dim][bold]broadcom-wps:[/bold] hostapd, niepodatny[/dim]"))
        else:
            self._log(treelog.branch("[dim][bold]broadcom-wps:[/bold] niejednoznaczne[/dim]"))

    def _log_identity_details(self, ap: AccessPoint) -> None:
        details = fm.router_identity_details(ap)
        if details:
            self._log_identity_details_text(details)

    def _log_identity_details_text(self, details: str) -> None:
        self._log("[bold]Tożsamość routera[/bold]")
        lines = [line for line in details.splitlines() if line]
        for i, line in enumerate(lines):
            connector = treelog.leaf if i == len(lines) - 1 else treelog.branch
            self._log(connector(line))

    # ----- command-bar (footer hotkeys) --------------------------------------

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Drive the footer keys off the same state as the buttons."""
        if self._is_probing():
            if action in ("campaign", "deauth_all", "wps_pbc_mode"):
                return False
        ap = self._target_ap
        if action == "campaign":
            if ap is None:
                return False
            key = parameters[0]
            if key == "chop":                      # WEP sub-action: enabled only while WEP runs (and not silenced)
                if not WepCampaign.visible(ap):
                    return False
                cur = self._controls.current
                running = cur is not None and cur.key == "wep"
                return None if (not running or Config.is_silenced(ap.bssid)) else True
            cls = fm.CAMPAIGN_BY_KEY.get(key)
            if cls is None or not cls.visible(ap):
                return False
            active = Campaign.active
            if active is not None and active.key == key and cls.stoppable:
                return True
            return None if fm.campaign_blocked(cls, ap) is not None else True
        if action == "deauth_all":
            if ap is None:
                return False
            return None if fm.deauth_blocked(ap) else True
        if action == "silence":
            return False if (ap is not None and Config.is_silenced(ap.bssid)) else True
        if action == "unsilence":
            return True if (ap is not None and Config.is_silenced(ap.bssid)) else False
        return True

    def _sync_bindings(self) -> None:
        """Repaint the footer only when a key's shown/greyed state changes."""
        ap = self._target_ap
        if ap is None:
            sig: Optional[tuple] = None
        else:
            active = Campaign.active
            probing = self._is_probing()

            def _disabled(cls) -> bool:
                if active is not None and active.key == cls.key and cls.stoppable:
                    return False
                return fm.campaign_blocked(cls, ap) is not None

            btn_sig = tuple((cls.key, cls.visible(ap), True if probing else _disabled(cls))
                            for cls in fm.BUTTON_CAMPAIGNS)
            sig = (btn_sig,
                   True if probing else fm.deauth_blocked(ap),
                   Config.is_silenced(ap.bssid),
                   probing)
        if sig != self._binding_sig:
            self._binding_sig = sig
            self.refresh_bindings()

    def action_campaign(self, camp_key: str) -> None:
        """Toggle a hotkey's campaign."""
        toggle = self._campaign_toggles.get(camp_key)
        if toggle is not None:
            toggle()
        self._sync_bindings()

    def action_deauth_all(self) -> None:
        """'d': one-shot broadcast deauth, matching the client-panel button."""
        self.run_worker(self._run_deauth_broadcast(), exclusive=True)

    def action_wps_pbc_mode(self) -> None:
        """'w': toggle the shared WPS PBC auto-invade."""
        self.app.pbc_enabled = not self.app.pbc_enabled
        Config.wps_auto_pbc = self.app.pbc_enabled
        self.app.persist_config()
        self._log_pbc_status()

    def _log_pbc_status(self) -> None:
        if self.app.pbc_enabled:
            self._log("[bold]Wyłuskiwanie WPS PushButton[/bold] "
                      "[bold green]włączone[/bold green] [dim](przełącz klawiszem w)[/dim]")
        else:
            self._log("[bold]Wyłuskiwanie WPS PushButton[/bold] "
                      "[yellow]wyłączone[/yellow] [dim](przełącz klawiszem w)[/dim]")

    def action_silence(self) -> None:
        self._toggle_silence()

    def action_unsilence(self) -> None:
        self._toggle_silence()

    def _toggle_silence(self) -> None:
        """Flip the focused AP's silenced state (campaigns off, handshakes/PMKIDs ignored)."""
        if self._target_ap is None:
            return
        bssid = self._target_ap.bssid.lower()
        if bssid in Config.silenced_bssids:
            Config.silenced_bssids.remove(bssid)
            self._log("[green bold] ● AP [italic]od[/italic]ciszone ✓[/]")
            self._log("[dim green]" + treelog.leaf("kampanie włączone, nasłuch handshake'ów") + "[/]")
        else:
            Config.silenced_bssids.append(bssid)
            self._log_silenced()
        self.app.persist_config()
        self.refresh_buttons()
        self._sync_bindings()

    def _log_silenced(self) -> None:
        self._log("[yellow bold] ● AP wyciszone[/] [red]✗S[/]")
        self._log("[yellow dim]" + treelog.branch("kampanie wyłączone, handshake'i ignorowane.") + "[/]")
        self._log("[yellow dim]" + treelog.leaf("wciśnij [bold]s[/bold], by odciszyć.") + "[/]")

    # ----- deauth ------------------------------------------------------------

    async def _run_deauth_broadcast(self) -> None:
        """Worker: broadcast-deauth every station associated with the focused AP."""
        ap = self._target_ap
        array = self.app.array
        card = array.select_iface(ap.channel) if (ap and array) else None
        if not ap or card is None:
            self._log("[red]✗ Brak celu / karty na tym kanale. Przerywam broadcast.[/red]")
            return
        self._log("[bold]Deauth broadcast: wszyscy klienci[/bold]")
        try:
            sent = await card.deauth_broadcast(ap.bssid, count=self._DEAUTH_BCAST_COUNT)
        except Exception as exc:
            logger.exception("Broadcast deauth crashed")
            self._log(treelog.leaf_fail(f"Broadcast nieudany: {escape(str(exc))}"))
            return
        # Broadcast frames are never ACKed: a neutral leaf (no green ✓).
        self._log(treelog.leaf(f"wysłano {sent} ramek deauth [dim](AP→broadcast)[/dim]"))

    async def _run_deauth_selected(self, mac: str) -> None:
        """Worker: deauth a specific client (the inline ✕ that was clicked)."""
        ap = self._target_ap
        array = self.app.array
        card = array.select_iface(ap.channel) if (ap and array) else None
        if not ap or card is None:
            self._log("[red]✗ Brak celu / karty na tym kanale. Przerywam deauth.[/red]")
            return
        self._log(f"[bold]Deautoryzacja klienta {escape(mac)}[/bold]")
        try:
            res = await card.deauth_client(ap.bssid, mac, rounds=self._DEAUTH_SEL_ROUNDS)
        except Exception as exc:
            logger.exception("Deauth %s crashed", mac)
            self._log(treelog.leaf_fail(f"Deauth nieudany: {escape(str(exc))}"))
            return
        self._log(treelog.branch(
            f"wysłano {res.total_sent} ramek deauth "
            f"[dim](AP↔klient ×{res.client_sent})[/dim]"))
        if not res.measured:  # card lacks TX-ACK detection, nothing to confirm
            self._log(treelog.leaf("[dim]dostarczenia nie mierzono (brak TX-ACK na tej karcie)[/dim]"))
            return
        detail = f"[dim](klient {res.client_acks}/{res.client_sent} · AP {res.ap_acks}/{res.ap_sent})[/dim]"
        if res.total_acked:
            self._log(treelog.leaf(
                f"[bold][cyan]{res.total_acked}[/cyan]/{res.total_sent} deauth z ACK[/bold] {detail}"))
        else:
            self._log(treelog.leaf(
                f"[bold][red]0[/red]/{res.total_sent} deauth z ACK[/bold] [dim](milczące AP i klient)[/dim]"))

    # ----- PMKID -------------------------------------------------------------

    def _toggle_decloak(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "decloak":
            self._controls.request_stop()
        else:
            self._start_decloak()
        self.refresh_buttons()

    def _start_decloak(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Przerywam odkrywanie.[/red]")
            return
        self.app.push_screen(DecloakInputModal(ap.bssid, array.guessid(ap)),
                             self._on_decloak_input)

    def _on_decloak_input(self, chosen: Optional[DecloakInput]) -> None:
        """The modal's result. Re-checks the target, since it may have been revealed or the
        radio taken while the dialog was open."""
        if chosen is None:
            return
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Przerywam odkrywanie.[/red]")
            return
        if not ap.is_hidden:
            self._log(treelog.branch("SSID zostało odkryte, gdy dialog był otwarty"))
            self.refresh_buttons()
            return
        self._log(treelog.header(f"Odkrywanie {escape(ap.bssid)}"))
        started = self._controls.start(DecloakCampaign, array, ap, decloak_input=chosen,
                                       log=lambda m: self._log(treelog.branch(m)))
        if started is None:
            self._log(treelog.branch("radio zajęte przez inną kampanię"))
        self.refresh_buttons()

    def _toggle_known_decloak(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == KnownDecloakCampaign.key:
            self._controls.request_stop()
            self.refresh_buttons()
            return
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Przerywam odkrywanie.[/red]")
            return
        self._log(treelog.header(f"Odkrywanie {escape(ap.bssid)}"))
        started = self._controls.start(KnownDecloakCampaign, array, ap,
                                       log=lambda m: self._log(treelog.branch(m)))
        if started is None:
            self._log(treelog.branch("radio zajęte przez inną kampanię"))
        self.refresh_buttons()

    def _toggle_pmkid(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "pmkid":
            self._controls.request_stop()
        else:
            self._start_pmkid()
        self.refresh_buttons()

    def _start_pmkid(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Przerywam zbieranie PMKID.[/red]")
            return
        self._log(pmkid_log.header(escape(ap.ssid or ap.bssid)))
        self._controls.start(PmkidHarvestAttack, array, ap,
                             log=lambda m: self._log(treelog.branch(m)))

    def _finish_decloak(self, camp) -> None:
        if camp.revealed:
            self._log(treelog.leaf_ok(f"odkryto [bold]{escape(camp.revealed)}[/bold]"))
            if Config.remember_decloak(camp.ap.bssid, camp.revealed):
                self.app.persist_config()
        else:
            self._log(treelog.leaf_fail(f"brak trafienia ({camp.sent} prób)"))

    _finish_known_decloak = _finish_decloak

    def _finish_pmkid(self, camp) -> None:
        """Handle a completed harvest."""
        if camp.pmkid:
            result = self.app.vault.save_pmkid(camp.target, camp.client_mac)
            hint = _save_line(result) if result is not None else None
            chip = saved_psk_chip(result.matches_saved_psk) if result is not None else None
            self._emit_lines(pmkid_log.verdict_success(hint, chip))
            # Detector skips forged MACs, so toast the active-harvest win here too.
            name = camp.target.ssid or camp.target.bssid
            body = (f"[bold]{escape(name)}[/bold] na kanale [bold]{camp.target.channel}[/bold] "
                    f"[dim bold](BSSID: {escape(camp.target.bssid)})[/dim bold]")
            self.notify(body, title="PMKID przechwycony (M1)", timeout=6)
        elif getattr(camp, "stopped", False):
            self._log(treelog.leaf_fail("[bright_red bold]Zbieranie zatrzymane[/]"))
        else:
            self._emit_lines(pmkid_log.verdict_failure(camp.fail_reason))

    # ----- Deauth ------------------------------------------------------------

    def _toggle_deauth(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "deauth":
            self._controls.request_stop()
        else:
            self._start_deauth()
        self.refresh_buttons()

    def _start_deauth(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Przerywam deauth.[/red]")
            return
        self._log(f"[bold]Deauth[/bold] [bold]{escape(ap.ssid or ap.bssid)}[/bold]: "
                  "wymuszam nowy handshake")
        self._controls.start(DeauthCampaign, array, ap, log=self._log)

    def _finish_deauth(self, camp) -> None:
        """Reap a completed deauth run. A captured handshake is saved+toasted by the
        always-on capture path (HandshakeCaptured); we only log the campaign's end."""
        if camp.captured:
            self._log("[bold green]✓ Deauth wymusił handshake do złamania[/bold green]")
        else:
            self._log("[bright_red bold]Deauth zatrzymany[/]")

    # ----- EvilTwin ----------------------------------------------------------

    def _toggle_eviltwin(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "eviltwin":
            self._controls.request_stop()
        else:
            self._start_eviltwin()
        self.refresh_buttons()

    def _start_eviltwin(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array or not array.members:
            self._log("[red]✗ Brak celu / interfejsu. Nie uruchomię EvilTwin.[/red]")
            return
        # Every member here is FAKE_MAC=NONE/UNIMPLEMENTED, so the modal's host Select
        # would be built empty and raise EmptySelectError at compose. Guarding on members
        # alone left the button live but the campaign unstartable -- a crash, not a no-op.
        if not any(_can_host(m) for m in array.members):
            self._log("[red]✗ Żadna karta nie podrobi MAC-a, więc żadna nie postawi bliźniaka."
                      " EvilTwin wymaga karty SPOOFABLE lub FIXED_MAC.[/red]")
            return
        self.app.push_screen(EvilTwinInputModal(ap, array.members), self._on_eviltwin_input)

    def _on_eviltwin_input(self, evil_input: Optional[EvilTwinInput]) -> None:
        """The modal's callback: build + run EvilTwin from its input (the one campaign
        with a pre-construction step, so it can't go through a plain toggle)."""
        if evil_input is None:
            return
        ap, array = self._target_ap, self.app.array
        if not ap or not array:
            return
        try:
            started = self._controls.start(EvilTwinCampaign, array, ap, evil_input=evil_input)
        except Exception as exc:
            logger.exception("EvilTwin start failed")
            self._log(f"[bold red]✗ Start EvilTwin nieudany:[/bold red] {escape(str(exc))}")
            return
        if started is None:
            return
        self._log(f"[bold cyan]EvilTwin[/bold cyan] [bold cyan]"
                  f"{escape(ap.ssid or ap.bssid)}[/bold cyan] aktywny na kanale {evil_input.twin_channel}"
                  f" [dim]({evil_input.twin_bssid})[/dim]")
        self._log(treelog.branch(f"[italic]przepinam klientów[/italic] [dim]na[/dim] kanale {ap.channel}"))
        self._log(treelog.leaf("[dim]czekam na auth klientów…[/dim]"))
        self.refresh_buttons()

    def _finish_eviltwin(self, camp) -> None:
        """Reap a finished EvilTwin (user-stopped, or captured and released the radio itself)."""
        if camp.captured:
            self._log("[bold green]✓ EvilTwin przechwycił handshake do złamania[/bold green]")
        else:
            self._log("[bold red]EvilTwin zatrzymany[/bold red]")
        array = self.app.array
        twin = None if camp.same_bssid or not array else array.access_points.get(camp.twin_bssid)
        for client_mac in list(twin.handshakes) if twin else ():
            result = self.app.vault.save_handshake(twin, client_mac)
            if result is not None:
                chip = saved_psk_chip(result.matches_saved_psk)
                if chip:
                    self._log(treelog.branch(chip))
                self._log(treelog.leaf(_save_line(result)))

    # ----- WEP: Generate IVs (Replay) + Chop ---------------------------------

    def _toggle_generate_ivs(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "wep":
            if cur.recovered_key is None:
                self._controls.request_stop()
            else:
                self._controls.stop()          # finished campaign lingering; clear then restart
                self._start_generate_ivs()
        else:
            self._start_generate_ivs()
        self.refresh_buttons()

    def _start_generate_ivs(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / interfejsu. Nie uruchomię generowania IV.[/red]")
            return
        try:
            self._controls.start(WepCampaign, array, ap, log=self._log)
        except Exception as exc:
            logger.exception("Generate IVs start failed")
            self._log(f"[bold red]✗ Start generowania IV nieudany:[/bold red] {escape(str(exc))}")

    def _finish_wep(self, camp) -> None:
        if camp.recovered_key is not None:
            result = self.app.vault.save_wep_key(camp.target, camp.recovered_key)
            if result is not None:
                self._log(treelog.leaf(_save_line(result)))

    def _toggle_chop(self) -> None:
        camp = self._controls.current
        if camp is None or camp.key != "wep":
            self._log("[yellow]Najpierw uruchom Replay[/yellow] [dim](ChopChop "
                      "wytwarza ziarno ARP dla silnika replay)[/dim]")
            return
        if camp.chop_active:
            camp.stop_chop()
            self._log("[cyan]→ Chop zatrzymany[/cyan] [dim](powrót do ARP replay)[/dim]")
        else:
            camp.start_chop()
        self.refresh_buttons()

    # ----- WPS PIN -----------------------------------------------------------

    def _toggle_wps_pin(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "wps":
            self._controls.request_stop()
        else:
            self._start_wps_pin()
        self.refresh_buttons()

    def _start_wps_pin(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ Brak celu / puli kart. Nie uruchomię WPS PIN.[/red]")
            return
        # Warn when the elected card can't HW-ACK a spoofed MAC (still runs PIN fine, just spammy).
        card = array.select_iface(ap.channel)
        if card is not None:
            warning = card.active_monitor_warning()
            if isinstance(warning, str):
                self._log(warning)
        self._launch_wps_pin(ap, array)

    def _launch_wps_pin(self, ap, array) -> None:
        try:
            name = escape(ap.ssid or ap.bssid)
            self._log(f"[bold]WPS PIN brute[/bold] ruszył na [bold cyan]{name}[/bold cyan]")
            self._controls.start(WpsCampaign, array, ap,
                                 log=lambda m: self._log(treelog.branch(m)))
        except Exception as exc:
            logger.exception("WPS PIN start failed")
            self._log(f"[bold red]✗ Start WPS PIN nieudany:[/bold red] {escape(str(exc))}")

    def _finish_wps(self, camp) -> None:
        """Reap a finished WPS PIN sweep: log/save the found PIN, else the give-up reason."""
        ssid = escape(camp.target.ssid or camp.bssid)
        if camp.state.found_pin is not None:
            camp.target.wps_pin = camp.state.found_pin
            camp.target.wps_pin_psk = camp.state.found_psk
            displayed_pin = camp.state.found_pin or EMPTY_PIN_LABEL
            self._log(treelog.branch_ok(
                f"[black bold on cyan]  WPS PIN dla {ssid}: "
                f"{escape(displayed_pin)}  [/black bold on cyan]"))
            self._log(treelog.branch(
                f"[black bold on green] Hasło dla {ssid}: "
                f"\"{escape(camp.state.found_psk or '')}\" [/black bold on green]"))
            try:
                result = self.app.vault.save_wps_pin(
                    camp.target, camp.state.found_pin, camp.state.found_psk)
                if result is None:
                    self._log(treelog.leaf("[dim](nie zapisano)[/dim]"))
                else:
                    self._log(treelog.leaf(_save_line(result)))
            except Exception:
                self._log(treelog.leaf("[dim](nie zapisano)[/dim]"))
        elif getattr(camp, "fail_reason", None):
            self._log(treelog.leaf_fail(
                f"[bold red]poddaję się:[/bold red] [yellow]{escape(camp.fail_reason)}[/yellow]"))
        else:
            self._log(treelog.leaf(
                f"[yellow]WPS PIN zatrzymany[/yellow] "
                f"[dim]({camp.state.tested} testów, faza {camp.state.phase})[/dim]"))

    # ----- WPS PBC auto-capture ----------------------------------------------

    def _start_pbc_capture(self, ap) -> None:
        array = self.app.array
        if not array:
            return
        self._log("[bold cyan]WPS PushButton:[/bold cyan] [bold green]okno otwarte[/bold green] "
                  "(auto-przechwyt PSK)")
        self._controls.start(WpsPbcCampaign, array, ap,
                             log=lambda m: self._log(treelog.branch(m)))

    def _finish_pbc(self, camp) -> None:
        """Handle a completed PBC attempt."""
        ap = camp.target
        if getattr(camp, "stopped", False):
            self._log(treelog.leaf(
                "[yellow]zatrzymano[/yellow] [dim](radio wolne; auto-invade wróci przy "
                "następnym oknie)[/dim]"))
            return
        if camp.error is not None:
            self._log(treelog.leaf_fail(f"błąd przechwytu: {escape(str(camp.error))}"))
            return
        outcome = camp.outcome
        if outcome is None:
            return
        if outcome.result is PinResult.SUCCESS:
            ap.wps_pbc_psk = outcome.psk
            name = escape(outcome.ssid or ap.ssid or ap.bssid)
            password = (f"[black bold on green] Hasło dla {name}: "
                        f"\"{escape(outcome.psk)}\" [/black bold on green]")
            try:
                result = self.app.vault.save_wps_pbc(ap, outcome.psk)
            except Exception:
                result = None
            if result is None:
                self._log(treelog.branch(password))
                self._log(treelog.leaf("[dim](nie zapisano)[/dim]"))
            else:
                self._log(treelog.leaf(password))
        else:
            self._pbc_retry_after = time.monotonic() + _PBC_RETRY_COOLDOWN_S
            self._log(treelog.leaf_warn(
                f"{outcome.result.value} [dim]({escape(outcome.detail)})[/dim], "
                f"ponawiam za {_PBC_RETRY_COOLDOWN_S:.0f}s, dopóki okno otwarte"))

    def _toggle_pbc(self) -> None:
        cur = self._controls.current
        if cur is not None and cur.key == "pbc":
            self._pbc_user_stopped = True
            self._controls.request_stop()
        else:
            self._start_pbc_capture(self._target_ap)

    # ----- Verify ------------------------------------------------------------

    def _verify_pressed(self, button: Button) -> None:
        keys = self._verify_keys(self._target_ap)
        if len(keys) == 1:
            self._start_verify(keys[0])
        elif keys:
            self.app.push_screen(VerifyKeyMenu(keys, offset=(button.region.x, button.region.bottom)),
                                 self._start_verify)

    def _start_verify(self, key: Optional[Key]) -> None:
        ap = self._target_ap
        array = self.app.array
        if key is None or ap is None or array is None:
            return
        campaign = VERIFY_CAMPAIGNS[key.type]
        self._log(f"Weryfikuję {CREDENTIAL_NAMES[key.type]} [cyan]{escape(key_text(key))}[/]…")
        started = self._controls.start(campaign, array, ap, verify_value=key.value,
                                       log=lambda m: self._log(treelog.branch(m)))
        if started is None:
            self._log(treelog.leaf("radio zajęte przez inną kampanię"))
            return
        self._verify_key = key
        self.refresh_buttons()

    def _finish_verify(self, camp: VerifyCampaign) -> None:
        if camp.stopped or camp.outcome is None:
            self._log(treelog.leaf("[yellow]Zatrzymano[/yellow]"))
            return
        if camp.outcome is VerifyOutcome.VERIFIED:
            self._log(treelog.leaf("[green]Poprawne[/green]"))
        elif camp.outcome is VerifyOutcome.REJECTED:
            self._log(treelog.leaf("[red]Błędne (nieaktualne)[/red]"))
        else:
            self._log(treelog.leaf(f"[yellow]Błąd:[/yellow] {escape(camp.error or '')}"))
            return
        self.app.vault.record_live_verification(self._verify_key, correct=camp.outcome is VerifyOutcome.VERIFIED)

    # ----- navigation --------------------------------------------------------

    async def action_go_back(self) -> None:
        # Tear down any running attack: Scanner doesn't own the AP's channel, and a forged daemon would keep injecting.
        campaign = self._controls.current
        self._controls.stop()
        if campaign is not None:
            await campaign.stop()      # teardown finishes before Scanner resumes hopping
        self._stop_probe()
        ap = self._target_ap
        logger.info("[FOCUS] leave: ssid=%r bssid=%s",
                    getattr(ap, "ssid", None), getattr(ap, "bssid", None))
        self.app.pop_screen()
