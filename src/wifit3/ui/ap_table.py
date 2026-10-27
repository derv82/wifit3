"""Scanner AP list: a Line-API table fed plain values, repainting only the lines that moved.

Textual's DataTable keys every render cache on a table-global update counter, so one changed
cell re-renders every visible cell. Here a row is a frozen dataclass of plain values; the table
diffs those, rebuilds only the cells whose values moved, and refreshes only those terminal lines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from rich.console import ConsoleOptions
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip

from .encryption_format import EncryptionSummary

# Cap the SSID+badges cell so the capture badges never overflow the column.
SSID_CELL_MAX = 32

IDENTITY_CELL_MAX = 24
"""Cap for the VENDOR/ID cell, same role as SSID_CELL_MAX for the SSID cell."""
# 1 space of padding on each side of every cell.
_CELL_PADDING = 1

# Animated jumps (home/end, re-sort) take this long whatever the distance. Textual's
# default is a speed, not a duration, so a long list crawled for over a second.
SCROLL_DURATION_S = 0.25


@dataclass(frozen=True, slots=True)
class APRow:
    """One scanner row as plain comparable values; the table supplies the colour."""
    bssid: str
    ssid: Optional[str] = None
    sibling_ssid: Optional[str] = None
    channel: int = 0
    signal: int = -100
    beacons: int = 0
    clients: int = 0
    encryption: Optional[EncryptionSummary] = None
    wps: bool = False
    wps_locked: bool = False
    chipset: str = ""
    identity: str = ""
    is_stale: bool = False
    beacon_flash: bool = False
    silenced: bool = False
    has_handshake: bool = False
    has_pmkid: bool = False
    has_wep_key: bool = False
    has_wps_psk: bool = False


# (APRow attribute, colour, glyph) for the badges left of the SSID.
_BADGES = (
    ("silenced", "red", "✗S"),
    ("has_handshake", "green", "✓HS"),
    ("has_pmkid", "green", "✓PMK"),
    ("has_wep_key", "green", "✓WEP"),
    ("has_wps_psk", "green", "✓WPS"),
)


def _render_ssid(row: APRow, fg: str) -> Text:
    if row.ssid:
        name = Text(row.ssid, style=f"{fg} bold")
    else:
        shown = f"{row.sibling_ssid}?" if row.sibling_ssid else "<Hidden>"
        name = Text(shown, style=f"{fg} italic")

    badges = Text()
    for attr, colour, glyph in _BADGES:
        if getattr(row, attr):
            if badges:
                badges.append(" ")
            badges.append(glyph, style=colour)

    reserved = 1 + badges.cell_len if badges else 0
    name.truncate(max(1, SSID_CELL_MAX - reserved), overflow="ellipsis")
    if not badges:
        name.justify = "right"
        return name
    badges.append(" ")
    badges.append_text(name)
    badges.justify = "right"
    return badges


def _render_channel(row: APRow, fg: str) -> Text:
    return Text(str(row.channel), justify="right", style=fg)


def _render_signal(row: APRow, fg: str) -> Text:
    return Text(f"{row.signal} dBm", justify="right", style=fg)


def _render_beacons(row: APRow, fg: str) -> Text:
    style = f"{fg} bold" if row.beacon_flash else fg
    return Text(str(row.beacons), justify="right", style=style)


def _render_clients(row: APRow, fg: str) -> Text:
    return Text(str(row.clients) if row.clients else "", justify="right", style=fg)


def _render_encryption(row: APRow, fg: str) -> Text:
    markup = row.encryption.markup(muted=fg) if row.encryption else ""
    return Text.from_markup(markup, emoji=False, style=fg)


def _render_wps(row: APRow, fg: str) -> Text:
    if not row.wps:
        return Text("", style=fg)
    return Text("WPS 🔒" if row.wps_locked else "WPS", style=fg)


def _render_identity(row: APRow, fg: str) -> Text:
    # Cap like the SSID cell does. manufacturer/model/device_type are uncapped WPS and
    # probe-response strings, and _relayout sizes a column from the widest row, so one
    # verbose AP pushed the table past the terminal and every other column off-screen.
    text = Text(row.identity, style=fg)
    text.truncate(IDENTITY_CELL_MAX, overflow="ellipsis")
    return text


def _render_chipset(row: APRow, fg: str) -> Text:
    return Text(row.chipset, style=fg)


def _secondary_signal(row: APRow, reverse: bool) -> int:
    """Tie-break that keeps the stronger AP on top whichever way the column sorts."""
    return row.signal if reverse else -row.signal


def _sink_empty(empty: bool, reverse: bool) -> int:
    """Leading sort field that keeps blank cells at the bottom in both directions."""
    return int(empty != reverse)


def _ssid_key(row: APRow, reverse: bool) -> tuple:
    name = row.ssid or ""
    return (_sink_empty(not name, reverse), name.lower(),
            _secondary_signal(row, reverse), row.bssid)


def _channel_key(row: APRow, reverse: bool) -> tuple:
    return (1 if reverse else 0, row.channel, _secondary_signal(row, reverse), row.bssid)


def _signal_key(row: APRow, reverse: bool) -> tuple:
    clients = row.clients if reverse else -row.clients
    return (1 if reverse else 0, row.signal, clients, row.bssid)


def _beacons_key(row: APRow, reverse: bool) -> tuple:
    return (1 if reverse else 0, row.beacons, _secondary_signal(row, reverse), row.bssid)


def _clients_key(row: APRow, reverse: bool) -> tuple:
    return (_sink_empty(row.clients == 0, reverse), row.clients,
            _secondary_signal(row, reverse), row.bssid)


def _encryption_key(row: APRow, reverse: bool) -> tuple:
    raw = row.encryption.raw if row.encryption else ""
    empty = not raw or raw == "UNKNOWN"
    return (_sink_empty(empty, reverse), raw.lower(),
            _secondary_signal(row, reverse), row.bssid)


def _wps_key(row: APRow, reverse: bool) -> tuple:
    rank = (1 if row.wps_locked else 2) if row.wps else 0
    return (_sink_empty(rank == 0, reverse), rank,
            _secondary_signal(row, reverse), row.bssid)


def _chipset_key(row: APRow, reverse: bool) -> tuple:
    return (_sink_empty(not row.chipset, reverse), row.chipset.lower(),
            _secondary_signal(row, reverse), row.bssid)


def _identity_key(row: APRow, reverse: bool) -> tuple:
    return (_sink_empty(not row.identity, reverse), row.identity.lower(),
            _secondary_signal(row, reverse), row.bssid)


@dataclass(frozen=True)
class Column:
    """A column's label, the APRow fields its cell reads, and how it renders and sorts."""
    key: str
    label: str
    fields: Tuple[str, ...]
    render: Callable[[APRow, str], Text]
    sort_key: Callable[[APRow, bool], tuple]
    right_aligned: bool = False
    min_width: int = 0


# Order here = on-screen order. ``fields`` is the cache key: a cell is rebuilt only
# when one of its fields moves, so every input to its appearance must be listed.
COLUMNS: Tuple[Column, ...] = (
    Column("ssid", "SSID",
           ("ssid", "sibling_ssid", "silenced", "has_handshake", "has_pmkid",
            "has_wep_key", "has_wps_psk", "is_stale"),
           _render_ssid, _ssid_key, right_aligned=True, min_width=20),
    Column("channel", "CH", ("channel", "is_stale"),
           _render_channel, _channel_key, right_aligned=True),
    Column("signal", "POWER", ("signal", "is_stale"),
           _render_signal, _signal_key, right_aligned=True),
    Column("beacons", "🥓", ("beacons", "beacon_flash", "is_stale"),
           _render_beacons, _beacons_key, right_aligned=True),
    Column("clients", "💻", ("clients", "is_stale"),
           _render_clients, _clients_key, right_aligned=True),
    Column("encryption", "ENCRYPT", ("encryption", "is_stale"),
           _render_encryption, _encryption_key),
    Column("wps", "WPS", ("wps", "wps_locked", "is_stale"),
           _render_wps, _wps_key),
    Column("chipset", "CHIPSET", ("chipset", "is_stale"),
           _render_chipset, _chipset_key),
    Column("identity", "VENDOR/ID", ("identity", "is_stale"),
           _render_identity, _identity_key),
)

COLUMN_KEYS: Tuple[str, ...] = tuple(column.key for column in COLUMNS)
# +2 reserves the sort indicator, so flipping the sort never resizes a column.
_HEADER_WIDTHS: Tuple[int, ...] = tuple(Text(column.label).cell_len + 2 for column in COLUMNS)
_BY_KEY: Dict[str, Column] = {column.key: column for column in COLUMNS}
_CLIENTS_INDEX = COLUMN_KEYS.index("clients")
_CHIPSET_INDEX = COLUMN_KEYS.index("chipset")


@dataclass(slots=True)
class _RowEntry:
    """Per-row caches. ``texts`` feeds column measurement; ``segments`` depends on the
    measured width, so a width change invalidates segments but not texts."""
    data: APRow
    texts: List[Optional[Text]]
    widths: List[int]
    segments: List[Optional[List[Segment]]]
    strip: Optional[Strip]

    @classmethod
    def new(cls, row: APRow) -> "_RowEntry":
        size = len(COLUMNS)
        return cls(row, [None] * size, [0] * size, [None] * size, None)


class APTable(ScrollView, can_focus=True):
    """AP list table. Feed it `set_rows`; it owns column widths, sort order and the cursor."""

    COMPONENT_CLASSES = {"ap-table--header", "ap-table--cursor"}

    DEFAULT_CSS = """
    APTable {
        background: transparent;
        color: $foreground;
        width: 100%;
        height: 1fr;

        & > .ap-table--header {
            background: $panel;
            color: $foreground;
            text-style: bold;
        }
        & > .ap-table--cursor {
            background: $block-cursor-blurred-background;
            color: $block-cursor-blurred-foreground;
            text-style: $block-cursor-blurred-text-style;
        }
        &:focus > .ap-table--cursor {
            background: $block-cursor-background;
            color: $block-cursor-foreground;
            text-style: $block-cursor-text-style;
        }
    }
    """

    BINDINGS = [
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("pageup", "cursor_page_up", "Page up", show=False),
        Binding("pagedown", "cursor_page_down", "Page down", show=False),
        Binding("enter", "select_row", "Select", show=False),
    ]

    sort_column: reactive[str] = reactive(COLUMN_KEYS[2], init=False)
    sort_reverse: reactive[bool] = reactive(True, init=False)

    class RowSelected(Message, namespace="ap_table"):
        """The user activated a row (enter or click)."""
        def __init__(self, bssid: str) -> None:
            self.bssid = bssid
            super().__init__()

    class SortChanged(Message, namespace="ap_table"):
        """The user re-sorted by clicking a column header."""
        def __init__(self, column: str, reverse: bool) -> None:
            self.column = column
            self.reverse = reverse
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: Dict[str, _RowEntry] = {}
        self._order: List[str] = []
        self._index: Dict[str, int] = {}
        self._widths: List[int] = [0] * len(COLUMNS)
        self._shown: Tuple[int, ...] = tuple(range(len(COLUMNS)))
        self._options_by_width: Dict[int, ConsoleOptions] = {}
        self._header: Optional[Strip] = None
        self._dirty: List[str] = []
        self._widths_stale = True
        self._cursor_bssid: Optional[str] = None
        self._foreground: str = ""

    # ----- Public surface ----------------------------------------------------

    @property
    def row_count(self) -> int:
        return len(self._order)

    @property
    def ordered_bssids(self) -> List[str]:
        return list(self._order)

    @property
    def cursor_bssid(self) -> Optional[str]:
        return self._cursor_bssid

    @property
    def cursor_row(self) -> int:
        """Display index of the cursor, or -1 when the table is empty."""
        if self._cursor_bssid is None:
            return -1
        return self._index.get(self._cursor_bssid, -1)

    def row_data(self, bssid: str) -> Optional[APRow]:
        entry = self._rows.get(bssid)
        return entry.data if entry else None

    def set_rows(self, rows: Iterable[APRow]) -> None:
        """Replace the row set. Only cells whose values moved are rebuilt, and only
        the lines holding them are repainted."""
        incoming = {row.bssid: row for row in rows}
        dropped = [bssid for bssid in self._rows if bssid not in incoming]
        for bssid in dropped:
            del self._rows[bssid]          # may have held a column's widest cell
            self._widths_stale = True
        structural = bool(dropped)
        dirty: List[str] = []

        for bssid, row in incoming.items():
            entry = self._rows.get(bssid)
            if entry is None:
                self._rows[bssid] = _RowEntry.new(row)
                self._dirty.append(bssid)
                structural = True
            elif entry.data is not row and entry.data != row:
                self._invalidate_changed_cells(entry, row)
                self._dirty.append(bssid)
                dirty.append(bssid)

        if structural:
            self._reindex()
        repaint = self._relayout()
        if repaint or structural:
            self.refresh()
        else:
            for bssid in dirty:
                index = self._index.get(bssid)
                if index is not None:
                    self.refresh_line(index + 1)

    def resort(self) -> bool:
        """Re-order rows by the current sort column. True if the order moved.
        The cursor tracks its AP, so the highlight follows without moving the viewport."""
        column = _BY_KEY[self.sort_column]
        reverse = self.sort_reverse
        order = sorted(
            self._rows,
            key=lambda bssid: column.sort_key(self._rows[bssid].data, reverse),
            reverse=reverse,
        )
        if order == self._order:
            return False
        self._order = order
        self._index = {bssid: index for index, bssid in enumerate(order)}
        self.refresh()
        return True

    def move_cursor(self, row: int, *, scroll: bool = True, animate: bool = False) -> None:
        """Put the cursor on display row ``row`` (clamped)."""
        if not self._order:
            return
        row = max(0, min(row, len(self._order) - 1))
        bssid = self._order[row]
        if bssid != self._cursor_bssid:
            previous = self._index.get(self._cursor_bssid) if self._cursor_bssid else None
            self._cursor_bssid = bssid
            if previous is not None:
                self.refresh_line(previous + 1)
            self.refresh_line(row + 1)
        if scroll:
            self.scroll_cursor_into_view(animate=animate)

    def scroll_cursor_into_view(self, *, animate: bool = False) -> None:
        row = self.cursor_row
        height = self.size.height - 1          # the header is pinned to line 0
        if row < 0 or height <= 0:
            return
        top = self.scroll_offset.y
        if row < top:
            target = row
        elif row >= top + height:
            target = row - height + 1
        else:
            return
        self.scroll_to(y=target, animate=animate, force=True,
                       duration=SCROLL_DURATION_S if animate else None)

    def invalidate(self) -> None:
        """Drop every render cache and rebuild from the row values."""
        self._drop_caches()
        self._relayout()
        self.refresh()

    # ----- Diffing / layout --------------------------------------------------

    def _drop_caches(self) -> None:
        size = len(COLUMNS)
        self._dirty = list(self._rows)
        self._widths_stale = True
        for entry in self._rows.values():
            entry.texts = [None] * size
            entry.segments = [None] * size
            entry.strip = None
        self._header = None

    def _invalidate_changed_cells(self, entry: _RowEntry, row: APRow) -> None:
        old = entry.data
        for index, column in enumerate(COLUMNS):
            if any(getattr(old, name) != getattr(row, name) for name in column.fields):
                entry.texts[index] = None
                entry.segments[index] = None
        entry.data = row
        entry.strip = None

    def _reindex(self) -> None:
        """Keep the established order, append arrivals; re-sorting is on the caller's cadence."""
        cursor_row = self.cursor_row
        known = set(self._order)
        self._order = [bssid for bssid in self._order if bssid in self._rows]
        self._order += [bssid for bssid in self._rows if bssid not in known]
        self._index = {bssid: index for index, bssid in enumerate(self._order)}
        if self._cursor_bssid not in self._rows:
            fallback = max(0, min(cursor_row, len(self._order) - 1))
            self._cursor_bssid = self._order[fallback] if self._order else None

    def _relayout(self) -> bool:
        """Build dirty cell Texts and re-measure the columns. True if every line must repaint."""
        repaint = self._sync_foreground()

        rebuilt = False
        for bssid in self._dirty:
            entry = self._rows.get(bssid)
            if entry is None:
                continue
            for index, column in enumerate(COLUMNS):
                if entry.texts[index] is None:
                    text = column.render(entry.data, self._foreground)
                    if entry.data.is_stale:
                        text.stylize("dim")
                    entry.texts[index] = text
                    entry.widths[index] = text.cell_len
                    rebuilt = True
        self._dirty.clear()

        if not (rebuilt or repaint or self._widths_stale):
            self.virtual_size = Size(self._content_width(), len(self._order) + 1)
            return False
        self._widths_stale = False

        widths = [
            max(column.min_width, _HEADER_WIDTHS[index],
                max((entry.widths[index] for entry in self._rows.values()), default=0))
            for index, column in enumerate(COLUMNS)
        ]
        widths[0] = self._ssid_width_fitting_clients(widths)
        shown = self._shown_columns(widths)
        if widths != self._widths or shown != self._shown:
            self._widths = widths
            self._shown = shown
            self._header = None
            for entry in self._rows.values():
                entry.segments = [None] * len(COLUMNS)
                entry.strip = None
            repaint = True

        self.virtual_size = Size(self._content_width(), len(self._order) + 1)
        return repaint

    def _content_width(self) -> int:
        return sum(self._widths[index] + 2 * _CELL_PADDING for index in self._shown)

    def _shown_columns(self, widths: List[int]) -> Tuple[int, ...]:
        """Every column, less CHIPSET when the full row would overflow the visible width."""
        every = tuple(range(len(COLUMNS)))
        visible_width = self.scrollable_content_region.width
        if not visible_width or sum(widths) + 2 * _CELL_PADDING * len(widths) <= visible_width:
            return every
        return tuple(index for index in every if index != _CHIPSET_INDEX)

    def _ssid_width_fitting_clients(self, widths: List[int]) -> int:
        visible_width = self.scrollable_content_region.width
        if not visible_width:
            return widths[0]
        columns_after_ssid = widths[1:_CLIENTS_INDEX + 1]
        width_after_ssid = sum(columns_after_ssid) + 2 * _CELL_PADDING * len(columns_after_ssid)
        width_left_for_ssid = visible_width - width_after_ssid - 2 * _CELL_PADDING
        return max(COLUMNS[0].min_width, min(widths[0], width_left_for_ssid))

    def _refit_ssid_width(self) -> None:
        self._widths_stale = True
        if self._relayout():
            self.refresh()

    def on_resize(self) -> None:
        self._refit_ssid_width()

    def watch_show_vertical_scrollbar(self) -> None:
        self._refit_ssid_width()

    def _sync_foreground(self) -> bool:
        foreground = self.app.theme_variables.get("foreground", "#ffffff")
        if foreground == self._foreground:
            return False
        self._foreground = foreground
        self._drop_caches()
        return True

    # ----- Rendering ---------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        scroll_x, scroll_y = self.scroll_offset
        base = self.rich_style
        if y == 0:
            header = base + self._component_style("ap-table--header")
            return (self._header_strip()
                    .apply_style(header)
                    .crop(scroll_x, scroll_x + width)
                    .extend_cell_length(width, header))

        index = y + scroll_y - 1
        if not 0 <= index < len(self._order):
            return Strip.blank(width, base)
        strip = (self._row_strip(index)
                 .apply_style(base)
                 .crop(scroll_x, scroll_x + width)
                 .extend_cell_length(width, base))
        if self._order[index] == self._cursor_bssid:
            strip = self._highlight(strip)
        return strip

    def _highlight(self, strip: Strip) -> Strip:
        """Cursor row: the selection's colours win over the cell's, so a themed highlight
        can never swallow a cell painted the same colour."""
        cursor = self._component_style("ap-table--cursor")
        return Strip(list(Segment.apply_style(strip, post_style=cursor)), strip.cell_length)

    def _row_strip(self, index: int) -> Strip:
        entry = self._rows[self._order[index]]
        if entry.strip is None:
            entry.strip = Strip(self._join_cells(entry.segments, entry.texts)).simplify()
        return entry.strip

    def _header_strip(self) -> Strip:
        if self._header is None:
            arrow = "▼" if self.sort_reverse else "▲"
            cells = []
            for column in COLUMNS:
                mark = arrow if column.key == self.sort_column else " "
                if column.right_aligned:
                    cells.append(Text(f"{mark} {column.label}", justify="right"))
                else:
                    cells.append(Text(f"{column.label} {mark}", justify="left"))
            self._header = Strip(self._join_cells([None] * len(COLUMNS), cells)).simplify()
        return self._header

    def _join_cells(
        self, cache: List[Optional[List[Segment]]], texts: List[Optional[Text]]
    ) -> List[Segment]:
        pad = Segment(" " * _CELL_PADDING)
        segments: List[Segment] = []
        for index in self._shown:
            cell = cache[index]
            if cell is None:
                cell = self._render_cell(texts[index], self._widths[index])
                cache[index] = cell
            segments.append(pad)
            segments.extend(cell)
            segments.append(pad)
        return segments

    def _render_cell(self, text: Text, width: int) -> List[Segment]:
        return self.app.console.render_lines(text, self._options(width), pad=True)[0]

    def _options(self, width: int) -> ConsoleOptions:
        options = self._options_by_width.get(width)
        if options is None:
            options = self.app.console.options.update(
                width=width, height=1, no_wrap=True, overflow="ellipsis"
            )
            self._options_by_width[width] = options
        return options

    def _component_style(self, name: str) -> Style:
        return self.get_component_styles(name).rich_style

    # ----- Reactions ---------------------------------------------------------

    def watch_sort_column(self) -> None:
        self._header = None
        self.resort()
        self.refresh()
        self.scroll_cursor_into_view(animate=True)

    def watch_sort_reverse(self) -> None:
        self._header = None
        self.resort()
        self.refresh()
        self.scroll_cursor_into_view(animate=True)

    # ----- Input -------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.move_cursor(max(self.cursor_row, 0) + 1)

    def action_cursor_up(self) -> None:
        self.move_cursor(max(self.cursor_row, 0) - 1)

    def action_cursor_page_down(self) -> None:
        self.move_cursor(max(self.cursor_row, 0) + max(1, self.size.height - 1))

    def action_cursor_page_up(self) -> None:
        self.move_cursor(max(self.cursor_row, 0) - max(1, self.size.height - 1))

    def action_select_row(self) -> None:
        if self._cursor_bssid is not None:
            self.post_message(self.RowSelected(self._cursor_bssid))

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        if offset.y == 0:
            self._sort_by_column_at(offset.x + self.scroll_offset.x)
            return
        index = offset.y + self.scroll_offset.y - 1
        if not 0 <= index < len(self._order):
            return
        self.move_cursor(index)
        if event.chain == 2:
            self.post_message(self.RowSelected(self._order[index]))

    def _sort_by_column_at(self, x: int) -> None:
        edge = 0
        for index in self._shown:
            column = COLUMNS[index]
            edge += self._widths[index] + 2 * _CELL_PADDING
            if x < edge:
                if column.key == self.sort_column:
                    self.sort_reverse = not self.sort_reverse
                else:
                    self.sort_column = column.key
                self.post_message(self.SortChanged(self.sort_column, self.sort_reverse))
                return
