import pytest
from textual.app import App
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import RichLog

from wifit3.ui.selectable_rich_log import SelectableRichLog
from wifit3.ui.app import WifiteApp


def test_selectable_rich_log_inherits_rich_log():
    log = SelectableRichLog()
    assert isinstance(log, RichLog)


async def test_selectable_rich_log_coordinate_mapping():
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        log.write("Alpha Bravo Charlie")
        await pilot.pause(0.05)

        widget, offset = app.screen.get_widget_and_offset_at(6, 0)
        assert widget is log
        assert offset == Offset(x=6, y=0)


async def test_selectable_rich_log_extracts_single_and_multiline_selection():
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        log.write("First line of log")
        log.write("Second line of log")
        await pilot.pause(0.05)

        # Single line selection: 'line'
        sel_single = Selection.from_offsets(Offset(6, 0), Offset(10, 0))
        text_single, ending = log.get_selection(sel_single)
        assert text_single == "line"
        assert ending == "\n"

        # Multi-line selection
        sel_multi = Selection.from_offsets(Offset(6, 0), Offset(6, 1))
        text_multi, ending = log.get_selection(sel_multi)
        assert text_multi == "line of log\nSecond"


async def test_selectable_rich_log_out_of_bounds_selection_does_not_crash():
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        for i in range(5):
            log.write(f"Line {i}")
        await pilot.pause(0.05)

        # Selection completely in void past end of content (user stack trace scenario)
        sel_void = Selection(start=Offset(2, 5), end=Offset(5, 5))
        text, ending = log.get_selection(sel_void)
        assert text == ""

        sel_void_drag = Selection(start=Offset(149, 5), end=Offset(2, 6))
        text, ending = log.get_selection(sel_void_drag)
        assert text == ""

        # Drag from valid content into void
        sel_into_void = Selection(start=Offset(2, 3), end=Offset(5, 7))
        text, ending = log.get_selection(sel_into_void)
        assert text == "ne 3\nLine 4"

        # Empty lines below content do not receive coordinate mapping
        strip_void = log.render_line(5)
        assert not any(seg.style and "offset" in seg.style.meta for seg in strip_void._segments)


async def test_selectable_rich_log_visual_selection_styling():
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        log.write("Hello World")
        await pilot.pause(0.05)

        # Before selection: segments are normal
        strip_before = log.render_line(0)
        assert not any("0178d4" in str(seg.style) for seg in strip_before._segments)

        # Trigger selection from x=0 to x=5
        await pilot.mouse_down(log, offset=(0, 0))
        await pilot.hover(log, offset=(5, 0))
        await pilot.pause(0.05)

        strip_selected = log.render_line(0)
        selected_segs = [seg for seg in strip_selected._segments if seg.text.startswith("Hello")]
        assert selected_segs
        assert any("0178d4" in str(seg.style) for seg in selected_segs)
        assert all(seg.style is None or seg.style.color != seg.style.bgcolor for seg in selected_segs)
        assert strip_selected.cell_length >= 11

        await pilot.mouse_up(log, offset=(5, 0))


@pytest.mark.usefixtures("no_usb_devices")
async def test_wifite_app_copies_selection_to_clipboard_and_notifies():
    app = WifiteApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.push_screen("scanner")
        await pilot.pause(0.05)

        log = app.screen.query_one("#system-log", SelectableRichLog)
        log.clear()
        log.write("Found WPA2 handshake key: secret-password-123")
        await pilot.pause(0.05)

        toasts = []
        app.notify = lambda message, *args, **kwargs: toasts.append(message)

        # Drag select 'secret-password-123' (starts at column 26, length 19 -> ends at 45)
        # offset y=1 accounts for border-top
        await pilot.mouse_down(log, offset=(26, 1))
        await pilot.hover(log, offset=(45, 1))
        await pilot.mouse_up(log, offset=(45, 1))
        await pilot.pause(0.05)

        assert app.clipboard == "secret-password-123"
        assert len(toasts) == 1
        assert toasts[0] == "Copied 19 chars to clipboard"


@pytest.mark.usefixtures("no_usb_devices")
async def test_wifite_app_single_click_does_not_toast():
    app = WifiteApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.push_screen("scanner")
        await pilot.pause(0.05)

        log = app.screen.query_one("#system-log", SelectableRichLog)
        log.clear()
        log.write("Some line in the log")
        await pilot.pause(0.05)

        toasts = []
        app.notify = lambda message, *args, **kwargs: toasts.append(message)

        # Single click in place
        await pilot.mouse_down(log, offset=(5, 1))
        await pilot.mouse_up(log, offset=(5, 1))
        await pilot.pause(0.05)

        assert len(toasts) == 0


async def test_selectable_rich_log_offsets_follow_cursor_during_active_drag():
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        log.write("Hello beautiful world of textual")
        await pilot.pause(0.05)

        await pilot.mouse_down(log, offset=(0, 0))
        for x in range(1, 16):
            await pilot.hover(log, offset=(x, 0))
            widget, offset = app.screen.get_widget_and_offset_at(x, 0)
            assert widget is log
            assert offset == Offset(x, 0)
        await pilot.mouse_up(log, offset=(15, 0))


async def test_selectable_rich_log_identical_fg_bg_guard(monkeypatch):
    class _App(App):
        def compose(self):
            yield SelectableRichLog(id="log")

    app = _App()
    async with app.run_test(size=(80, 20)) as pilot:
        log = app.query_one("#log", SelectableRichLog)
        log.write("Guarded text")
        await pilot.pause(0.05)

        from rich.style import Style
        monkeypatch.setattr(app.screen, "get_component_rich_style", lambda *a, **kw: Style(color="blue", bgcolor="blue"))

        await pilot.mouse_down(log, offset=(0, 0))
        await pilot.hover(log, offset=(5, 0))
        await pilot.pause(0.05)

        strip = log.render_line(0)
        selected_segs = [seg for seg in strip._segments if seg.text.startswith("Guard")]
        assert selected_segs
        assert all(seg.style is None or seg.style.color != seg.style.bgcolor for seg in selected_segs)
        await pilot.mouse_up(log, offset=(5, 0))


@pytest.mark.usefixtures("no_usb_devices")
async def test_selectable_rich_log_double_click_does_not_select_all():
    app = WifiteApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        app.push_screen("scanner")
        await pilot.pause(0.05)

        log = app.screen.query_one("#system-log", SelectableRichLog)
        log.clear()
        log.write("Some long line in the log that should not be selected entirely")
        await pilot.pause(0.05)

        toasts = []
        app.notify = lambda message, *args, **kwargs: toasts.append(message)

        await pilot.double_click(log, offset=(5, 1))
        await pilot.pause(0.05)

        assert app.screen.get_selected_text() is None
        assert len(toasts) == 0


