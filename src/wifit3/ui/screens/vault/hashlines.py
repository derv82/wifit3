from __future__ import annotations

from itertools import groupby
from typing import Mapping, Optional

from rich.markup import escape
from textual.content import Content
from textual.events import MouseMove
from textual.widgets import Button, SelectionList
from textual.widgets.selection_list import Selection

from wifit3.persist.ap_state import DatedHashline
from wifit3.persist.exhaustion import ExhaustedWordlist
from wifit3.ui.screens.vault.rows import row_content, verdict_column, verdict_tooltip
from wifit3.vault.tools.base import VaultTool


HashlineGroup = tuple[DatedHashline, ...]


def _client_text(hashline: DatedHashline) -> str:
    """Last two bytes of the client MAC, ``…aa:bb``."""
    mac_sta = hashline.line.mac_sta
    return f"…{mac_sta[-4:-2]}:{mac_sta[-2:]}"


def hashline_groups(hashlines: tuple[DatedHashline, ...]) -> list[HashlineGroup]:
    """Runs of hashlines from the same minute, message pair, client and SSID."""
    def same(h: DatedHashline) -> tuple:
        minute = None if h.first_captured is None else h.first_captured // 60
        return minute, h.line.pair_label, h.line.mac_sta, h.line.ssid, h.line.authorized
    return [tuple(group) for _, group in groupby(hashlines, same)]


def hashlines_count_markup(count: int, total: int) -> str:
    return f"[dim]({count}/{total} hashlines)[/]"


def selected_count_text(count: int) -> str:
    return f"{count} hashline{'' if count == 1 else 's'} selected"


def _wordlists_text(count: int) -> str:
    return f"{count} wordlist{'' if count == 1 else 's'}"


def exhausted_count_text(group: HashlineGroup,
                         exhausted: Mapping[str, frozenset[ExhaustedWordlist]]) -> str:
    """``2 wordlists exhausted (1 on some lines)``: the wordlists that ran out on the group's lines."""
    per_line = [exhausted.get(h.id, frozenset()) for h in group]
    on_any = frozenset.union(*per_line)
    on_some = on_any - frozenset.intersection(*per_line)
    if not on_any:
        return ""
    return f"{_wordlists_text(len(on_any))} exhausted" + (f" ({len(on_some)} on some lines)" if on_some else "")


def _exhausted_names(group: HashlineGroup, exhausted: Mapping[str, frozenset[ExhaustedWordlist]]) -> list[str]:
    wordlists = frozenset.union(frozenset(), *(exhausted.get(h.id, frozenset()) for h in group))
    return sorted(" / ".join(w.names) or "unnamed wordlist" for w in wordlists)


def _hashline_tooltip(group: HashlineGroup, exhausted: Mapping[str, frozenset[ExhaustedWordlist]]) -> str:
    tooltip = verdict_tooltip(group[0].line.authorized)
    names = _exhausted_names(group, exhausted)
    if names:
        tooltip += f"\n\n[$text-warning]Exhausted {_wordlists_text(len(names))}:[/]\n" + "\n".join(f"- {escape(n)}" for n in names)
    return tooltip


def hashline_prompt(group: HashlineGroup, show_client: bool, show_ssid: bool,
                    exhausted_count: str, exhausted_width: int) -> Content:
    first = group[0]
    verdict = verdict_column(first.line.authorized)
    trailing = [((f"×{len(group)}" if len(group) > 1 else "").ljust(3), "$foreground-muted")]
    if exhausted_width:
        trailing.append((exhausted_count.ljust(exhausted_width), "italic $text-accent"))
    if show_ssid:
        trailing.append((first.line.ssid, ""))
    return row_content(first.first_captured, first.line.pair_label, _client_text(first), verdict, show_client,
                       trailing)


class CrackerButton(Button):
    """Opens ``tool``'s launch modal for the ticked hashlines."""

    def __init__(self, tool: VaultTool) -> None:
        super().__init__(tool.label, "primary", classes="tool-btn")
        self.tool = tool


class UncrackedHashlineList(SelectionList[HashlineGroup]):
    """The uncracked hashline groups, each ticked to crack or not."""

    def __init__(self, classes: str) -> None:
        super().__init__(classes=classes)
        self._exhausted: Mapping[str, frozenset[ExhaustedWordlist]] = {}

    def show(self, selections: list[Selection[HashlineGroup]],
             exhausted: Mapping[str, frozenset[ExhaustedWordlist]]) -> None:
        """Replace the options, highlighting the first; posts no SelectedChanged."""
        self._exhausted = exhausted
        self.tooltip = None
        with self.prevent(SelectionList.SelectedChanged):
            self.clear_options()
            self.add_options(selections)
        self.action_first()

    def on_mouse_move(self, event: MouseMove) -> None:
        index = event.style.meta.get("option")
        tooltip = None if index is None else _hashline_tooltip(self.get_option_at_index(index).value, self._exhausted)
        if tooltip != self.tooltip:
            self.tooltip = tooltip

    def groups(self) -> list[HashlineGroup]:
        return [self.get_option_at_index(i).value for i in range(self.option_count)]

    def highlighted_group(self) -> Optional[HashlineGroup]:
        return None if self.highlighted is None else self.get_option_at_index(self.highlighted).value
