from rich.text import Text

from wifit3.ui.screens.focus_v2.packet_dashboard import PacketDashboard


def test_unchanged_footer_does_not_repaint(mocker):
    dashboard = PacketDashboard([])
    repaint = mocker.patch.object(dashboard, "_repaint")
    footer = [Text("status")]

    dashboard.set_footer(footer)
    dashboard.set_footer([Text("status")])

    repaint.assert_called_once_with()
