"""WiFFy: the Clippy-homage assistant that slides in during a Windows WinUSB install/uninstall.

The wdi-simple.exe install can sit at 2% for minutes with nothing to show, so WiFFy fills the wait:
he slides in from the right of the progress modal, a chat bubble appears, and he slow-types a rotating
set of messages until the elevated op finishes. On completion (or error) he slides back out the way he
came. Windows-only in practice (Linux setup is ~200ms), but the widget itself is platform-free.

Structure is two layers so the "breathing" frame-swap never wipes the typed line:
  #wiffy-sprite  the .ans art (wiffy + yellow bubble outline); cycles chat1<->chatN to look alive
  #wiffy-text    an absolute-positioned Label parked over the bubble's hollow interior; slow-typed

Only WiffyAssistant.enter()/exit() are called from outside (via the modal's show/hide_assistant).
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from rich.text import Text
from textual.containers import Container
from textual.geometry import Offset
from textual.reactive import reactive
from textual.widgets import Label, Static

from wifit3.ui.ansi_art import make_black_transparent

_ASSETS = Path(__file__).parent / "assets" / "wiffy"

# Frame canvas size (every wiffy_*.ans is 50x20). The bubble's hollow interior, measured in cells
# from chat1.ans (cols 22..44, rows 1..14): the typed-text Label is parked here. All chat*.ans MUST
# keep the bubble at these exact cells, or the swap-to-breathe frame de-aligns the text overlay.
_FRAME_W, _FRAME_H = 50, 20
_HOLE_X, _HOLE_Y, _HOLE_W, _HOLE_H = 22, 1, 23, 14


def _load(name: str) -> Text:
    return make_black_transparent(Text.from_ansi((_ASSETS / name).read_text(encoding="utf-8")))


def _frames() -> tuple[Text, Text, list[Text]]:
    """(nochat, chat1, [chat2, chat3, ...]). nochat = boxless slide-in frame; chat1 = resting frame
    with the bubble; the rest are 'breathing' variants auto-discovered from wiffy_chat*.ans."""
    chats = sorted(p.name for p in _ASSETS.glob("wiffy_chat*.ans"))
    base = "wiffy_chat1.ans"
    alts = [_load(n) for n in chats if n != base]
    return _load("wiffy_nochat.ans"), _load(base), alts


# Each message is a tuple of lines; they render joined by a blank line ("\n\n"), so pre-break lines
# to <=~21 cols (the bubble is 23 wide) and keep to <=7 elements (a message renders 2N-1 rows and
# the bubble is 14 tall). GREETING plays once on entry; MESSAGES rotate (shuffled, no repeat until
# the pack is exhausted). Tune wording/spacing live with scripts/wiffy_preview.py ([space] = next).
_INSTALL_GREETING = ("Hi, I'm WiFFy!", "\nIt looks like", "you are trying to", "HACK THE PLANET!!!", "", "I can help!")
_INSTALL_MESSAGES = [
    ('\nI can help to recover', '"your" password from', '"your" router.', '', 'Just ask me how!'),
    ('\nSee Wifite on Netflix:', 'How To Sell Drugs Online:\n  Season 2 Episode 2\n  (6:20)', 'Youngbloods:\n  Season 2 Episode 1\n  (49:00)'),
    ('', 'If you were on Linux,', "you'd be cracking", 'PMKIDs by now.'),
    ('', 'I only exist because', 'installing WinUSB', 'is extremely slow.', '', '..up to 5 minutes!'),
    ('\nThe first rule of', 'wifit3 club is:', '', 'DO NOT TALK ABOUT', 'WIFIT3 CLUB'),
    ('', 'I want to apologize', 'in advance for', 'dropping handshakes.'),
    ('Remember:', '', 'Only audit networks', 'that you own!', '', '                lol'),
    # ('Everyone asks', '"WiFi?"', '', 'but nobody asks', '"HowFi?"'),
    # ('Did you know?', '', 'The 2.4GHz wave is', '~6 in / 12.5cm long.', '', '5GHz is half that:', '~3in / 6cm.'),
]
_UNINSTALL_GREETING = ("You did it? You hacked", "the ENTIRE planet?", "", "Restoring driver now...")
_UNINSTALL_MESSAGES = [
    ('Your card now goes', 'back to being a', 'boring Wi-Fi card.', '', 'ZeroCool would be', 'so disappointed!'),
    ("I'll be here when", 'you want to', 'HACK THE PLANET again'),
    ("You'll be back", "they always come back"),
]

INSTALL_LINES = (_INSTALL_GREETING, _INSTALL_MESSAGES)
UNINSTALL_LINES = (_UNINSTALL_GREETING, _UNINSTALL_MESSAGES)


class WiffyAssistant(Container):
    """The animated assistant. Owns its own slide/typing/breathing timers; tears them all down on
    exit() (or on removal). Positioned absolutely at the modal's bottom-right and slid via offset."""

    DEFAULT_CSS = f"""
    WiffyAssistant {{
        layer: wiffy; position: absolute;
        width: {_FRAME_W}; height: {_FRAME_H};
        background: transparent;
    }}
    WiffyAssistant #wiffy-sprite {{ width: {_FRAME_W}; height: {_FRAME_H}; }}
    WiffyAssistant #wiffy-text {{
        position: absolute; offset: {_HOLE_X} {_HOLE_Y};
        width: {_HOLE_W}; height: {_HOLE_H};
        background: #ffff00; color: black;   /* classic Clippy; matches the baked border's yellow */
    }}
    """

    # 0.0 = fully off the right edge, 1.0 = resting spot. Textual can't tween the offset style
    # directly (it's a ScalarOffset), so animate this float and map it to offset in the watcher.
    slide = reactive(0.0)

    MARGIN_X, MARGIN_Y = 1, 0     # cells kept clear of the screen's right/bottom edge
    TYPE_INTERVAL = 0.05          # seconds per typed character
    HOLD = 5.0                    # pause after a fully-typed line before the next
    WIGGLE_MIN, WIGGLE_MAX = 4.0, 8.0   # random gap between "breathing" twitches
    WIGGLE_HOLD = 1.0             # how long an alt frame shows before snapping back to chat1

    def __init__(self, greeting: tuple[str, ...], messages: list[tuple[str, ...]]) -> None:
        super().__init__()
        self._nochat, self._chat1, self._alts = _frames()
        self._greeting = greeting
        self._messages = messages
        self._queue: list[int] = []
        self._entered = False
        self._exiting = False
        self._dialogue = None
        self._wiggle_timer = None
        self._rest = Offset(0, 0)
        self._hidden = Offset(0, 0)

    def compose(self):
        yield Static(self._nochat, id="wiffy-sprite")   # boxless until the slide settles
        yield Label("", id="wiffy-text")

    def on_mount(self) -> None:
        self._recompute_anchors()

    def reposition(self) -> None:
        """Re-pin to the lower-right for the current screen size. The host modal calls this on
        terminal resize; safe mid-slide (a running animation just tweens toward the new rest)."""
        if self.is_mounted:
            self._recompute_anchors()

    def _recompute_anchors(self) -> None:
        w, h = self.screen.size
        self._rest = Offset(max(0, w - _FRAME_W - self.MARGIN_X), max(0, h - _FRAME_H - self.MARGIN_Y))
        self._hidden = Offset(w + 2, self._rest.y)      # fully clear of the right edge
        self._apply_slide()

    def watch_slide(self, value: float) -> None:
        self._apply_slide()

    def _apply_slide(self) -> None:
        x = round(self._hidden.x + (self._rest.x - self._hidden.x) * self.slide)
        self.styles.offset = (x, self._rest.y)

    async def enter(self, *, intro_delay: float = 2.0) -> None:
        """Wait, slide in from the right, reveal the bubble, then start typing + breathing."""
        await asyncio.sleep(intro_delay)
        if self._exiting:
            return
        self.animate("slide", value=1.0, duration=0.5, easing="out_back")
        await asyncio.sleep(0.5)
        if self._exiting:
            return
        self.query_one("#wiffy-sprite", Static).update(self._chat1)   # bubble pops in, in place
        self._entered = True
        self._schedule_wiggle()
        self._dialogue = self.run_worker(self._run_dialogue(), name="wiffy-dialogue")

    async def exit(self, *, ok: bool = True) -> None:
        """Stop everything and slide back out the way he came (fast + sharp on error)."""
        self._exiting = True
        if self._dialogue is not None:
            self._dialogue.cancel()
        if self._wiggle_timer is not None:
            self._wiggle_timer.stop()
        if self._entered:
            dur = 0.4 if ok else 0.22
            self.animate("slide", value=0.0, duration=dur, easing="in_cubic" if ok else "in_back")
            await asyncio.sleep(dur)
        await self.remove()

    async def _run_dialogue(self, greeting_first: bool = True) -> None:
        if greeting_first:
            await self._type(self._greeting)
            await asyncio.sleep(self.HOLD)
        while True:
            await self._type(self._next_message())
            await asyncio.sleep(self.HOLD)

    def skip(self) -> None:
        """Preview affordance ([space]): abandon the current line/pause and start typing the next
        message now. Cancels the dialogue worker and restarts it past the greeting."""
        if not self._entered or self._exiting:
            return
        if self._dialogue is not None:
            self._dialogue.cancel()
        self._dialogue = self.run_worker(
            self._run_dialogue(greeting_first=False), name="wiffy-dialogue")

    async def _type(self, lines: tuple[str, ...]) -> None:
        text = "\n" + "\n\n".join(lines)      # blank line between each, so short messages fill the bubble
        label = self.query_one("#wiffy-text", Label)
        label.update("")
        buf = ""
        for ch in text:
            buf += ch
            label.update(buf)
            await asyncio.sleep(self.TYPE_INTERVAL)

    def _next_message(self) -> tuple[str, ...]:
        if not self._queue:
            self._queue = list(range(len(self._messages)))
            random.shuffle(self._queue)
        return self._messages[self._queue.pop()]

    def _schedule_wiggle(self) -> None:
        if not self._alts:
            return
        self._wiggle_timer = self.set_timer(
            random.uniform(self.WIGGLE_MIN, self.WIGGLE_MAX), self._wiggle)

    def _wiggle(self) -> None:
        if self._exiting:
            return
        self.query_one("#wiffy-sprite", Static).update(random.choice(self._alts))
        self.set_timer(self.WIGGLE_HOLD, self._wiggle_back)

    def _wiggle_back(self) -> None:
        if self._exiting:
            return
        self.query_one("#wiffy-sprite", Static).update(self._chat1)
        self._schedule_wiggle()
