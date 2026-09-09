from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog


class SelectableRichLog(RichLog):
    """RichLog supporting text selection and coordinate mapping."""

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        virtual_y = scroll_y + y
        if virtual_y >= len(self.lines):
            return super().render_line(y)

        strip = super().render_line(y)

        selection = self.text_selection
        if selection is not None:
            select_span = selection.get_span(virtual_y)
            if select_span is not None:
                start, end = select_span
                start = max(0, start - scroll_x)
                end = strip.cell_length if end == -1 else min(strip.cell_length, end - scroll_x)
                if start < end:
                    selection_style = self.screen.get_component_rich_style(
                        "screen--selection", partial=True, default=Style(reverse=True)
                    )
                    if selection_style.color is not None and selection_style.color == selection_style.bgcolor:
                        selection_style = Style(bgcolor=selection_style.bgcolor)
                    if selection_style.bgcolor is None and selection_style.color is None:
                        selection_style = Style(reverse=True)
                    cuts = sorted(list(set(c for c in (start, end, strip.cell_length) if 0 < c < strip.cell_length) | {strip.cell_length}))
                    parts = strip.divide(cuts)
                    cur = 0
                    styled_parts = []
                    for part in parts:
                        part_end = cur + part.cell_length
                        if cur >= start and part_end <= end:
                            styled = Strip(list(Segment.apply_style(part._segments, post_style=selection_style)), part.cell_length)
                            styled_parts.append(styled)
                        else:
                            styled_parts.append(part)
                        cur = part_end
                    strip = Strip.join(styled_parts)
        return strip.apply_offsets(scroll_x, virtual_y)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self.lines:
            return None

        line_count = len(self.lines)
        if selection.start is None:
            start_y, start_x = 0, 0
        else:
            start_y, start_x = selection.start.transpose

        if selection.end is None:
            end_y = line_count - 1
            end_x = len(self.lines[-1].text)
        else:
            end_y, end_x = selection.end.transpose

        if start_y >= line_count or end_y < 0:
            return "", "\n"

        if start_y < 0:
            start_y, start_x = 0, 0
        if end_y >= line_count:
            end_y = line_count - 1
            end_x = len(self.lines[-1].text)

        if start_y > end_y or (start_y == end_y and start_x >= end_x):
            return "", "\n"

        if start_y == end_y:
            line_text = self.lines[start_y].text
            return line_text[start_x:end_x], "\n"

        result = [self.lines[start_y].text[start_x:]]
        for y in range(start_y + 1, end_y):
            result.append(self.lines[y].text)
        result.append(self.lines[end_y].text[:end_x])

        return "\n".join(result), "\n"
