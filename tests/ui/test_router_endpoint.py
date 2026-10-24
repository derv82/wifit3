from unittest.mock import Mock

import pytest
from textual.app import App, ComposeResult

from wifit3.ui.screens.focus_v2.router_endpoint import RouterEndpoint


class _Host(App):
    def compose(self) -> ComposeResult:
        yield RouterEndpoint(id="router")


_VALUES = dict(
    essid="Test network",
    bssid="aa:bb:cc:dd:ee:ff",
    channel=6,
    power_dbm=-48,
    signal=8.0,
    wps=False,
    has_m1=False,
    probing=False,
    probe_disabled=False,
    identity="",
    identity_details=None,
)


@pytest.mark.asyncio
async def test_unchanged_router_identity_skips_static_widget_updates():
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause(0)
        router = app.query_one(RouterEndpoint)
        router.update(**_VALUES)
        router.query_one = Mock(wraps=router.query_one)

        router.update(**_VALUES)

        router.query_one.assert_not_called()
